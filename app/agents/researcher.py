"""Agent 1 - RESEARCHER (LangGraph). Responsibility: classify the petition, check corpus scope, and gather every
binding provision by choosing tools in a bounded loop (A1). Output: a ResearchDossier handed to the adjudicator.

  classify -> scope --(in scope)--> plan <-> act  (loop bounded by steps, tool calls, wall-clock)
                    \--(out of scope)--> END
If the provider is unavailable (breaker open / retries exhausted) the loop degrades to a FIXED tool sequence and the
dossier is marked degraded, which forces the adjudicator onto the escalation path (A5)."""
from __future__ import annotations
import time
from typing import TypedDict, Any
from langgraph.graph import StateGraph, END
from app import metrics as M, llm_client as llm
from app.agents.tools import Toolbox, BudgetExceeded
from app.config import settings
from app.gateway import heuristic
from app.guardrails import spotlight
from app.telemetry import span

AGENT = "researcher"
TYPES = {"attendance", "re_evaluation", "re_registration", "medical_leave", "grade_grievance", "other"}


class RState(TypedDict, total=False):
    run_id: str
    petition: dict
    strategy: str
    feedback: str
    classification: dict
    ptype: str
    out_of_scope: Any
    obs: list
    steps: int
    started: float
    stop_reason: str
    degraded: bool
    next: Any


def _ctx(config):
    return config["configurable"]["ctx"]


def _obs_for_model(obs):
    """What the model sees of previous tool results (also what the token count is computed on)."""
    out = []
    for o in obs:
        d = {"tool": o["tool"], "args": o["args"]}
        if o.get("passages"):
            d["passages"] = [{"clause_id": p["clause_id"], "heading": p["heading"], "doc": p["doc"], "refs": p.get("refs", []),
                              "topics": p.get("topics", []), "text": p["text"]} for p in o["passages"]]
        if o.get("listing") is not None: d["listing"] = o["listing"]
        if o.get("fields"): d["fields"] = o["fields"]
        if o.get("error"): d["error"] = o["error"]
        out.append(d)
    return out


def n_classify(s: RState, config):
    c = _ctx(config)
    if s.get("classification"):
        return {}
    p = s["petition"]
    fields = {k: v for k, v in p.items() if k not in ("description", "student_ref")}
    payload = {"description": p["description"], "fields": fields}
    t0 = time.time()
    degraded, meta = False, {}
    try:
        out, meta = llm.chat(agent=AGENT, step="classify", prompt_name="classify", payload=payload, run_id=s["run_id"],
                             variables={"fields": fields, "description": spotlight(p["description"])}, deadline=c["deadline"])
    except (llm.LLMUnavailable, llm.LLMBadOutput) as e:
        out, degraded = heuristic.classify(payload), True      # rule-based keyword fallback, flagged
        meta = {"fallback": f"rules ({type(e).__name__})"}
    ptype = out.get("petition_type") if out.get("petition_type") in TYPES else "other"
    c["rec"].event(AGENT, "classify", output=out, latency_ms=round((time.time() - t0) * 1000, 1), degraded=degraded, **meta)
    return {"classification": out, "ptype": ptype, "degraded": s.get("degraded", False) or degraded}


def n_scope(s: RState, config):
    c = _ctx(config)
    scope = c["retriever"].meta.get("scope", {})
    p = s["petition"]
    reason = None
    if s["ptype"] == "other":
        reason = "petition type is outside the regulations in the corpus"
    elif scope and (p["program"] != scope.get("program") or p["admission_batch"] != scope.get("admission_batch")):
        reason = f"corpus covers {scope.get('program')} {scope.get('admission_batch')} batch; petition is {p['program']} {p['admission_batch']}"
    c["rec"].event(AGENT, "scope_check", in_scope=reason is None, reason=reason, corpus_scope=scope, latency_ms=0)
    return {"out_of_scope": reason}


def _fixed_plan(s: RState):
    """Degradation path: a fixed retrieval pipeline that needs no model."""
    obs = s.get("obs", [])
    ptype = s["ptype"]
    seq = [("get_petition_fields", {}),
           ("search_corpus", {"query": heuristic.BASE_QUERY.get(ptype, "") + " " + s["petition"]["description"][:120], "mode": "hybrid", "top_k": 4}),
           ("search_corpus", {"query": heuristic.BASE_QUERY.get(ptype, ""), "mode": "keyword", "top_k": 3, "doc": "BYE"})]
    if len(obs) < len(seq):
        t, a = seq[len(obs)]
        return {"tool": t, "args": a}, "fixed degraded pipeline step"
    return None, "fixed pipeline complete"


def n_plan(s: RState, config):
    c = _ctx(config)
    steps = s.get("steps", 0) + 1
    obs = s.get("obs", [])
    elapsed = time.time() - s["started"]
    if steps > settings.max_steps:
        return {"steps": steps, "next": None, "stop_reason": "max_steps"}
    if elapsed > settings.max_wall_s or time.time() > c["deadline"]:
        return {"steps": steps, "next": None, "stop_reason": "wall_clock"}
    tb: Toolbox = c["toolbox"]
    t0 = time.time()
    meta, degraded = {}, s.get("degraded", False)
    if c["strategy_mode"] == "single_pass":
        action, thought = (({"tool": "search_corpus", "args": {"query": s["petition"]["description"], "mode": "hybrid", "top_k": 4}}, "single-pass baseline")
                           if not obs else (None, "single-pass baseline: done"))
    elif degraded:
        action, thought = _fixed_plan(s)
    else:
        payload = {"observations": _obs_for_model(obs), "petition_type": s["ptype"], "strategy": s.get("strategy", "primary"),
                   "description": s["petition"]["description"], "budget": {"tool_calls": tb.max_calls - tb.calls, "steps": settings.max_steps - steps}}
        try:
            out, meta = llm.chat(agent=AGENT, step="research_plan", prompt_name="research_plan", payload=payload, run_id=s["run_id"],
                                 variables={"observations": payload["observations"], "petition_type": s["ptype"], "strategy": payload["strategy"],
                                            "budget": payload["budget"], "feedback": s.get("feedback") or "none"}, deadline=c["deadline"])
            action = None if out.get("finish") else out.get("action")
            thought = out.get("thought", "")
        except (llm.LLMUnavailable, llm.LLMBadOutput) as e:
            degraded = True
            action, thought = _fixed_plan(s)
            meta = {"fallback": f"fixed pipeline ({type(e).__name__}: {e})"}
    c["rec"].event(AGENT, "plan", step_no=steps, thought=thought, action=action, latency_ms=round((time.time() - t0) * 1000, 1), degraded=degraded, **meta)
    upd = {"steps": steps, "next": action, "degraded": degraded}
    if action is None:
        upd["stop_reason"] = "agent_finished"
    return upd


def n_act(s: RState, config):
    c = _ctx(config)
    a = s["next"]
    try:
        o = c["toolbox"].call(a.get("tool", ""), a.get("args", {}) or {})
    except BudgetExceeded:
        return {"next": None, "stop_reason": "max_tool_calls"}
    return {"obs": s.get("obs", []) + [o], "next": None}


def _after_plan(s: RState):
    return "act" if s.get("next") else END


def build_graph():
    g = StateGraph(RState)
    g.add_node("classify", n_classify)
    g.add_node("scope", n_scope)
    g.add_node("plan", n_plan)
    g.add_node("act", n_act)
    g.set_entry_point("classify")
    g.add_edge("classify", "scope")
    g.add_conditional_edges("scope", lambda s: END if s.get("out_of_scope") else "plan", {END: END, "plan": "plan"})
    g.add_conditional_edges("plan", _after_plan, {"act": "act", END: END})
    g.add_edge("act", "plan")
    return g.compile()


_GRAPH = None


def research(petition: dict, run_id: str, retriever, rec, strategy="primary", feedback="", prior: dict | None = None,
             deadline: float | None = None, strategy_mode: str | None = None) -> dict:
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    t0 = time.time()
    prior = prior or {}
    tb = Toolbox(retriever, petition, rec, settings.max_tool_calls, settings.tool_timeout_s)
    init: RState = {"run_id": run_id, "petition": petition, "strategy": strategy, "feedback": feedback,
                    "classification": prior.get("classification"), "ptype": prior.get("ptype"),
                    "obs": prior.get("obs", []) if strategy == "alt" else [], "steps": 0, "started": time.time(), "degraded": False}
    ctx = {"retriever": retriever, "rec": rec, "toolbox": tb, "deadline": deadline or (time.time() + settings.run_deadline_s),
           "strategy_mode": strategy_mode or settings.search_strategy}
    with span("agent.researcher", run_id=run_id, strategy=strategy):
        final = _GRAPH.invoke(init, config={"configurable": {"ctx": ctx}, "recursion_limit": 4 * settings.max_steps + 10})
    retrieved = {}
    for o in final.get("obs", []):
        for p in o.get("passages", []):
            retrieved[p["clause_id"]] = p
    dossier = {"run_id": run_id, "ptype": final.get("ptype"), "classification": final.get("classification"),
               "out_of_scope": final.get("out_of_scope"), "degraded": final.get("degraded", False),
               "stop_reason": final.get("stop_reason", "out_of_scope" if final.get("out_of_scope") else ""),
               "steps": final.get("steps", 0), "tool_calls": tb.calls, "obs": final.get("obs", []),
               "retrieved": retrieved, "strategy": strategy, "scope": retriever.meta.get("scope", {}), "corpus_version": retriever.corpus_version}
    lat = time.time() - t0
    M.agent_steps.labels(AGENT).observe(dossier["steps"])
    M.agent_tool_calls.labels(AGENT).observe(tb.calls)
    M.agent_task_latency.labels(AGENT).observe(lat)
    M.agent_tasks.labels(AGENT, "degraded" if dossier["degraded"] else "success").inc()
    rec.event(AGENT, "handoff", to="adjudicator", stop_reason=dossier["stop_reason"], steps=dossier["steps"], tool_calls=tb.calls,
              retrieved=sorted(retrieved), out_of_scope=dossier["out_of_scope"], degraded=dossier["degraded"], latency_ms=round(lat * 1000, 1))
    return dossier
