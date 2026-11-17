"""Agent 2 - ADJUDICATOR (LangGraph). Responsibility: turn the dossier into a cited eligibility position, filter it,
VERIFY it against the passages actually retrieved, and route it - or send it back to the researcher with a different
strategy, or escalate. It owns no retrieval tools: it can only judge what the researcher handed over.

  precheck --(out of scope | conflict | degraded)--> finalize
     \--> draft -> guard --(violation)--> finalize(escalate)
                     \--> verify --(pass)--> finalize
                               \--(fail, rounds left)--> handoff back to researcher (strategy=alt)
                               \--(fail, no rounds)--> finalize(escalate: unverifiable)"""
from __future__ import annotations
import random, re, time
from typing import TypedDict, Any
from langgraph.graph import StateGraph, END
from app import metrics as M, llm_client as llm
from app.agents import conflicts, verifier
from app.config import settings
from app.guardrails import filter_output
from app.gateway.rules import r_committee, quote
from app.telemetry import span

AGENT = "adjudicator"


class AState(TypedDict, total=False):
    run_id: str
    petition: dict
    dossier: dict
    round: int
    draft: Any
    verification: Any
    result: Any
    handoff: Any
    flags: list
    code: Any


def _ctx(config):
    return config["configurable"]["ctx"]


def _facts(s):
    """Petition fields + corpus scope: the facts a statement may contain besides cited text."""
    return {**s["petition"], **{f"corpus_{k}": v for k, v in (s["dossier"].get("scope") or {}).items()}}


def _passages_for_model(retrieved):
    return [{"clause_id": p["clause_id"], "doc": p["doc"], "heading": p["heading"], "text": p["text"]} for p in retrieved.values()]


def n_precheck(s: AState, config):
    c = _ctx(config)
    d, p = s["dossier"], s["petition"]
    if d.get("out_of_scope"):
        dr = {"route": "escalate-to-human", "eligibility_position": "undetermined", "basis": "Outside the indexed regulations.",
              "rationale": f"No answer is given because the {d['out_of_scope']}.", "citations": [], "escalation_reason": "out_of_corpus"}
        c["rec"].event(AGENT, "precheck", outcome="out_of_corpus", latency_ms=0)
        return {"draft": dr, "code": "out_of_corpus"}
    if d.get("degraded"):
        cites = [{"clause_id": cid, "quote": quote(x), "role": "evidence"} for cid, x in list(d["retrieved"].items())[:4]]
        dr = {"route": "escalate-to-human", "eligibility_position": "undetermined", "basis": "Automated reasoning unavailable; evidence only.",
              "rationale": "The model provider was unavailable, so no eligibility position was formed. The provisions retrieved by the fixed pipeline are attached for manual review.",
              "citations": cites, "escalation_reason": "degraded"}
        c["rec"].event(AGENT, "precheck", outcome="degraded", latency_ms=0)
        M.degraded_runs.labels("provider_unavailable").inc()
        return {"draft": dr, "code": "degraded"}
    cf = conflicts.detect(p, d["ptype"], list(d["retrieved"].values()))
    if cf:
        ids = [x["clause_id"] for x in cf["provisions"]]
        cites = [{"clause_id": i, "quote": quote(d["retrieved"][i], r"within \d+ days"), "role": "conflicting"} for i in ids]
        for i in list(ids):   # a modifier is never cited without the base rule it modifies
            for m in re.finditer(r"Notwithstanding Clause (\d{1,2}\.\d{1,2})", d["retrieved"][i]["text"]):
                b = f"{d['retrieved'][i]['doc']}-{m.group(1)}"
                if b in d["retrieved"] and b not in ids:
                    ids.append(b); cites.append({"clause_id": b, "quote": quote(d["retrieved"][b]), "role": "base"})
        if cf["precedence_clause"]:
            cites.append({"clause_id": cf["precedence_clause"], "quote": quote(d["retrieved"][cf["precedence_clause"]], "inconsistency"), "role": "precedence"})
        lims = " and ".join(f"{x['clause_id']} requires it within {x['limit_days']} days" for x in cf["provisions"])
        dr = {"route": "escalate-to-human", "eligibility_position": "undetermined", "basis": f"Provisions conflict on the {cf['subject']}.",
              "rationale": f"The {cf['subject']} matters here: the petition gives {cf['field']} = {cf['petition_value']} days; {lims}." +
                           (f" Under {cf['precedence_clause']}, an inconsistency between the Bye-Laws and the Academic Regulations shall be referred to the Controller of Examinations for a ruling, and neither shall be treated as prevailing until the ruling is issued." if cf["precedence_clause"] else ""),
              "citations": cites, "escalation_reason": "conflict"}
        c["rec"].event(AGENT, "precheck", outcome="conflict", conflict=cf, latency_ms=0)
        return {"draft": dr, "code": "conflict"}
    c["rec"].event(AGENT, "precheck", outcome="proceed", latency_ms=0)
    return {"code": None}


def n_draft(s: AState, config):
    c = _ctx(config)
    d, p = s["dossier"], s["petition"]
    fields = {k: v for k, v in p.items() if k not in ("description", "student_ref", "client_petition_ref")}
    pas = _passages_for_model(d["retrieved"])
    t0 = time.time()
    try:
        out, meta = llm.chat(agent=AGENT, step="draft_position", prompt_name="draft_position", run_id=s["run_id"], deadline=c["deadline"],
                             payload={"fields": fields, "petition_type": d["ptype"], "passages": pas},
                             variables={"fields": fields, "petition_type": d["ptype"], "passages": pas})
    except llm.LLMUnavailable as e:
        cites = [{"clause_id": cid, "quote": quote(x), "role": "evidence"} for cid, x in list(d["retrieved"].items())[:4]]
        dr = {"route": "escalate-to-human", "eligibility_position": "undetermined", "basis": "Automated reasoning unavailable; evidence only.",
              "rationale": "The model provider was unavailable, so no eligibility position was formed. The retrieved provisions are attached for manual review.",
              "citations": cites, "escalation_reason": "degraded"}
        c["rec"].event(AGENT, "draft_position", outcome="provider_unavailable", error=str(e), latency_ms=round((time.time() - t0) * 1000, 1))
        M.degraded_runs.labels("provider_unavailable").inc()
        return {"draft": dr, "code": "degraded"}
    except llm.LLMBadOutput as e:
        c["rec"].event(AGENT, "draft_position", outcome="bad_output", error=str(e), latency_ms=round((time.time() - t0) * 1000, 1))
        return {"draft": {"route": "invalid", "citations": [], "rationale": "", "basis": ""}, "code": None}
    c["rec"].event(AGENT, "draft_position", draft=out, latency_ms=round((time.time() - t0) * 1000, 1), **meta)
    return {"draft": out, "code": None}


def n_guard(s: AState, config):
    c = _ctx(config)
    v = filter_output(s["draft"])
    c["rec"].event(AGENT, "output_guard", violations=v, latency_ms=0)
    if v:
        for x in v:
            M.guardrail_events.labels("output", x.split(":")[0][:40]).inc()
        return {"code": "guardrail", "flags": s.get("flags", []) + ["output_filter_blocked"]}
    return {}


def n_verify(s: AState, config):
    c = _ctx(config)
    t0 = time.time()
    v = verifier.verify(s["draft"], s["dossier"]["retrieved"], _facts(s))
    M.verification.labels("pass" if v["passed"] else "fail").inc()
    c["rec"].event(AGENT, "verify", passed=v["passed"], checks=v["checks"], feedback=v["feedback"], round=s["round"],
                   latency_ms=round((time.time() - t0) * 1000, 2))
    if v["passed"]:
        return {"verification": v}
    if s["round"] <= settings.max_verify_retries:
        return {"verification": v, "handoff": {"strategy": "alt", "feedback": v["feedback"]}}
    return {"verification": v, "code": "unverifiable"}


def _escalation(route, code, draft, cited_passages, dossier):
    if route == "refer-to-committee":
        comm = next((r_committee(p) for p in cited_passages if r_committee(p)), None) or \
               next((m.group(1) for p in cited_passages for m in [re.search(r"((?:[A-Z][a-z]+ )+Committee)", p["text"])] if m), "the competent committee")
        comp = next((p["clause_id"] for p in dossier["retrieved"].values() if comm in p["text"] and "shall consist of" in p["text"]), None)
        return {"required": True, "reason_code": "committee_reserved", "reason": f"The regulations reserve this decision to the {comm}.",
                "path": ["Examination Officer reviews this recommendation and the cited clauses",
                         f"Refer to the {comm}" + (f" (composition: {comp})" if comp else ""),
                         "Committee decision recorded and communicated by the Examination Section"]}
    if route == "escalate-to-human":
        reasons = {"conflict": "Provisions conflict; the officer must obtain a ruling before any position is taken.",
                   "out_of_corpus": "The petition is outside the indexed regulations; no answer is given.",
                   "unverifiable": "The drafted position could not be verified against the retrieved provisions.",
                   "guardrail": "The output filter blocked the drafted position.",
                   "degraded": "Automated reasoning was unavailable (degradation path); evidence only.",
                   "insufficient_evidence": "The retrieved provisions do not support any position."}
        rc = code or draft.get("escalation_reason") or "unverifiable"
        path = ["Examination Officer handles this petition manually"]
        if rc == "conflict":
            path.append("Seek a ruling from the Controller of Examinations (Bye-Laws precedence clause)")
        if rc == "out_of_corpus":
            path.append("Route to the section that owns this matter, or to the Academic Council for matters not provided for")
        return {"required": True, "reason_code": rc, "reason": reasons.get(rc, str(rc)), "path": path}
    return {"required": False, "reason_code": None, "reason": None, "path": ["Examination Officer confirms the cited provision and decides"]}


def n_finalize(s: AState, config):
    c = _ctx(config)
    d, code = s["dossier"], s.get("code")
    draft = dict(s.get("draft") or {})
    if code in ("guardrail", "unverifiable"):
        why = "the output filter blocked the drafted position" if code == "guardrail" else "the drafted position failed verification against the retrieved provisions"
        draft = {"route": "escalate-to-human", "eligibility_position": "undetermined", "basis": "No verified position.",
                 "rationale": f"No position is returned because {why}.", "citations": [], "escalation_reason": code}
    ver = s.get("verification")
    if ver is None or code in ("guardrail", "unverifiable"):
        ver2 = verifier.verify(draft, d["retrieved"], _facts(s))   # every returned answer is verified, including escalations
        if ver is not None:
            ver2["previous_failures"] = ver.get("feedback")
        ver = ver2
        c["rec"].event(AGENT, "verify", passed=ver["passed"], checks=ver["checks"], feedback=ver["feedback"], round=s["round"], final=True, latency_ms=0)
    if not ver["passed"]:  # never return an unverified answer, even a system-built one
        draft = {"route": "escalate-to-human", "eligibility_position": "undetermined", "basis": "No verified position.",
                 "rationale": "No position is returned because it could not be verified.", "citations": [], "escalation_reason": "unverifiable"}
        code = "unverifiable"
    cites = []
    for ci in draft.get("citations", []):
        p = d["retrieved"].get(ci["clause_id"])
        if p:
            cites.append({"clause_id": p["clause_id"], "doc_title": p["doc_title"], "chapter": p["chapter"], "chapter_title": p["chapter_title"],
                          "heading": p["heading"], "quote": ci["quote"], "role": ci.get("role", "supporting")})
    cited_passages = [d["retrieved"][x["clause_id"]] for x in cites]
    route = draft["route"]
    res = {"run_id": s["run_id"], "route": route, "eligibility_position": draft.get("eligibility_position", "undetermined"),
           "petition_type": d.get("ptype"), "basis": draft.get("basis", ""), "rationale": draft.get("rationale", ""),
           "citations": cites, "escalation": _escalation(route, code, draft, cited_passages, d),
           "verification": {"passed": ver["passed"], "rounds": s["round"], "checks": ver["checks"], "previous_failures": ver.get("previous_failures")},
           "degraded": code == "degraded" or d.get("degraded", False), "decision_authority": "examination_officer",
           "disclaimer": "Recommendation only. The examination officer decides this petition.",
           "prompt_versions": settings.prompt_versions, "corpus_version": d.get("corpus_version"),
           "guardrail_flags": s.get("flags", []), "research": {"steps": d.get("steps"), "tool_calls": d.get("tool_calls"),
                                                             "stop_reason": d.get("stop_reason"), "strategy": d.get("strategy")}}
    c["rec"].event(AGENT, "finalize", route=route, escalation=res["escalation"]["reason_code"], latency_ms=0)
    return {"result": res}


def build_graph():
    g = StateGraph(AState)
    for n, f in [("precheck", n_precheck), ("draft", n_draft), ("guard", n_guard), ("verify", n_verify), ("finalize", n_finalize)]:
        g.add_node(n, f)
    g.set_entry_point("precheck")
    g.add_conditional_edges("precheck", lambda s: "finalize" if s.get("code") else "draft", {"finalize": "finalize", "draft": "draft"})
    g.add_conditional_edges("draft", lambda s: "finalize" if s.get("code") else "guard", {"finalize": "finalize", "guard": "guard"})
    g.add_conditional_edges("guard", lambda s: "finalize" if s.get("code") else "verify", {"finalize": "finalize", "verify": "verify"})
    g.add_conditional_edges("verify", lambda s: END if s.get("handoff") else "finalize", {END: END, "finalize": "finalize"})
    g.add_edge("finalize", END)
    return g.compile()


_GRAPH = None


def adjudicate(petition: dict, run_id: str, dossier: dict, rnd: int, rec, deadline: float | None = None, flags: list | None = None) -> dict:
    """Returns {"result": RunResult-dict} or {"handoff": {"strategy", "feedback"}}."""
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_graph()
    t0 = time.time()
    ctx = {"rec": rec, "deadline": deadline or (time.time() + settings.run_deadline_s)}
    with span("agent.adjudicator", run_id=run_id, round=rnd):
        final = _GRAPH.invoke({"run_id": run_id, "petition": petition, "dossier": dossier, "round": rnd, "flags": flags or []},
                              config={"configurable": {"ctx": ctx}})
    lat = time.time() - t0
    M.agent_task_latency.labels(AGENT).observe(lat)
    if final.get("handoff"):
        M.agent_tasks.labels(AGENT, "handoff_retry").inc()
        rec.event(AGENT, "handoff", to="researcher", strategy="alt", feedback=final["handoff"]["feedback"], latency_ms=round(lat * 1000, 1))
        return {"handoff": final["handoff"]}
    r = final["result"]
    M.agent_tasks.labels(AGENT, "degraded" if r["degraded"] else ("escalated" if r["route"] == "escalate-to-human" else "success")).inc()
    return {"result": r}
