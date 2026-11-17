"""Deterministic offline provider. Implements the three model tasks (classify, research_plan, draft_position) from the
structured payload the agents send alongside the prompt text, plus fault injection used in the demonstration.
Token usage is estimated as len(text)/4 so cost accounting works identically to a real provider."""
from __future__ import annotations
import json, re, threading
from app.textutil import tokens
from app.gateway import rules as R

FAULT = {"mode": "none", "remaining": 0}   # set via gateway /admin/fault
_flock = threading.Lock()

TYPE_KEYWORDS = {
    "attendance": ["attendance", "shortfall", "condon", "detain", "classes", "absent", "hospitalis", "debar"],
    "re_evaluation": ["re-evaluat", "reevaluat", "revaluat", "recheck", "answer script", "re-total"],
    "re_registration": ["re-regist", "reregist", "backlog", "failed", "grade f", "repeat the course", "attempt"],
    "medical_leave": ["medical leave", "leave of absence", "leave for", "sick leave", "surgery", "recover"],
    "grade_grievance": ["grievance", "grade awarded", "unfair grade", "grading", "marks were", "wrongly graded"],
}
OTHER_KEYWORDS = ["branch change", "change my branch", "change of branch", "hostel", "scholarship", "transfer to", "fee refund",
                  "internship", "transcript", "bus pass", "library fine", "elective change", "placement"]
TYPE_FIELDS = {"attendance": ["attendance_percent", "classes_held"], "re_evaluation": [], "re_registration": ["grade_obtained", "attempts_used"],
               "medical_leave": ["leave_days"], "grade_grievance": []}
TYPE_TOPIC = {"attendance": "attendance", "re_evaluation": "re_evaluation", "re_registration": "re_registration",
              "medical_leave": "medical_leave", "grade_grievance": "grievance"}
BASE_QUERY = {"attendance": "minimum attendance required to appear for the end semester examination",
              "re_evaluation": "eligibility and time limit for re-evaluation of answer script",
              "re_registration": "re-registration for a course after grade F or detained",
              "medical_leave": "medical leave granted by head of department certificate",
              "grade_grievance": "grade grievance submission committee time limit"}
FACT_TERMS = {"course_type=lab": "laboratory courses", "course_type=project": "project courses", "grounds=sports_duty": "sports cultural event duty",
              "grounds=medical": "illness medical certificate", "medical_certificate_attached=False": "certificate required"}
MODIFIER_TERMS = "notwithstanding exception condonation relaxation shortfall may be condoned limit"


def classify(pl: dict) -> dict:
    desc = pl.get("description", "").lower()
    f = pl.get("fields", {})
    scores = {t: sum(2 for k in kws if k in desc) for t, kws in TYPE_KEYWORDS.items()}
    for t, fl in TYPE_FIELDS.items():
        scores[t] += sum(1 for x in fl if f.get(x) is not None)
    declared = f.get("declared_type")
    if declared in scores:
        scores[declared] += 3
    other_hit = any(k in desc for k in OTHER_KEYWORDS)
    best = max(scores, key=scores.get)
    if other_hit and scores[best] < 5:
        best, conf = "other", 0.8
    elif scores[best] == 0:
        best, conf = "other", 0.5
    else:
        tot = sum(scores.values())
        conf = round(scores[best] / tot, 2)
    m = re.search(r"(\d{1,3}(?:\.\d)?)\s*%", desc)
    return {"petition_type": best, "confidence": conf,
            "entities": {"attendance_percent": float(m.group(1)) if m else None, "course_code": f.get("course_code"),
                         "grounds": f.get("grounds"), "days_mentioned": None},
            "type_mismatch": bool(declared and best != declared and best != "other"),
            "notes": "keyword+field scoring" + ("; out-of-scope keywords present" if other_hit else "")}


def _seen(obs):
    out = {}
    for o in obs:
        for p in o.get("passages", []):
            out[p["clause_id"]] = p
    return out


def _did(obs, tool, **kw):
    return any(o["tool"] == tool and all(o["args"].get(k) == v for k, v in kw.items()) for o in obs)


def plan(pl: dict) -> dict:
    obs, ptype, strategy = pl["observations"], pl["petition_type"], pl.get("strategy", "primary")
    if pl.get("budget", {}).get("tool_calls", 1) <= 0:
        return {"thought": "Tool budget exhausted; finishing with what has been gathered.", "finish": True}
    if not _did(obs, "get_petition_fields"):
        return {"thought": "Read the validated petition fields first.", "action": {"tool": "get_petition_fields", "args": {}}}
    seen = _seen(obs)
    topic = TYPE_TOPIC.get(ptype)
    passages = list(seen.values())

    if strategy == "alt":   # verifier rejected the previous answer: enumerate the topic exhaustively instead of searching
        if not _did(obs, "list_clauses_by_topic", topic=topic):
            return {"thought": f"Previous evidence failed verification; enumerate every clause tagged {topic}.", "action": {"tool": "list_clauses_by_topic", "args": {"topic": topic}}}
        listed = [c["clause_id"] for o in obs if o["tool"] == "list_clauses_by_topic" for c in o.get("listing", [])]
        for cid in listed:
            if cid not in seen:
                return {"thought": f"Fetch {cid} listed under {topic}.", "action": {"tool": "get_clause", "args": {"clause_id": cid}}}
        if not _did(obs, "search_corpus", mode="keyword", doc="BYE"):
            return {"thought": "Keyword search of the Bye-Laws for the same subject.", "action": {"tool": "search_corpus", "args": {"query": BASE_QUERY[ptype], "mode": "keyword", "top_k": 3, "doc": "BYE"}}}
        return {"thought": "Exhaustive topic enumeration complete.", "finish": True}

    searches = [o for o in obs if o["tool"] == "search_corpus"]
    if not searches:
        kw = " ".join(dict.fromkeys(tokens(pl.get("description", ""), do_stem=False)))[:120]
        return {"thought": "Retrieve the provision that appears to apply.", "action": {"tool": "search_corpus", "args": {"query": f"{BASE_QUERY[ptype]} {kw}", "mode": "hybrid", "top_k": 4}}}
    base, _ = R.find(passages, R.BASE_READER[ptype])
    if not base and not _did(obs, "search_corpus", mode="keyword", topic=topic):
        return {"thought": "The base provision is not among the results; keyword search restricted to the topic.", "action": {"tool": "search_corpus", "args": {"query": BASE_QUERY[ptype], "mode": "keyword", "top_k": 4, "topic": topic}}}
    if not base:
        return {"thought": "No base provision could be found.", "finish": True}
    for ref in base.get("refs", []):
        if ref not in seen:
            return {"thought": f"{base['clause_id']} refers to {ref}; fetch it.", "action": {"tool": "get_clause", "args": {"clause_id": ref}}}
    if not _did(obs, "find_citing_clauses", clause_id=base["clause_id"]):
        return {"thought": f"Does any provision modify {base['clause_id']}? Find clauses that cite it.", "action": {"tool": "find_citing_clauses", "args": {"clause_id": base["clause_id"]}}}
    if not any(o["args"].get("purpose") == "modifier" for o in searches):
        derived = " ".join(dict.fromkeys(tokens(base["heading"] + " " + base["text"], do_stem=False)))[:120]
        f = next((o["fields"] for o in obs if o.get("fields")), {})
        facts = " ".join(FACT_TERMS.get(f"{k}={v}", "") for k, v in f.items()).strip()
        derived = (facts + " not eligible " + derived) if facts else derived
        return {"thought": f"Search again with a query derived from {base['clause_id']} for exceptions to it.",
                "action": {"tool": "search_corpus", "args": {"query": f"{MODIFIER_TERMS} {derived}", "mode": "hybrid", "top_k": 4, "purpose": "modifier"}}}
    modifiers = [p for p in passages if base["clause_id"] in p.get("refs", []) and p["clause_id"] != base["clause_id"]]
    for m in modifiers:
        if not _did(obs, "find_citing_clauses", clause_id=m["clause_id"]):
            return {"thought": f"{m['clause_id']} modifies {base['clause_id']}; find provisions that limit it in turn.", "action": {"tool": "find_citing_clauses", "args": {"clause_id": m["clause_id"]}}}
    if not _did(obs, "search_corpus", doc="BYE"):
        q = base["heading"] + " " + (" ".join(m["heading"] for m in modifiers)) + " time limit within days"
        return {"thought": "Check the Examination Bye-Laws for limits on the same subject.", "action": {"tool": "search_corpus", "args": {"query": q, "mode": "hybrid", "top_k": 3, "doc": "BYE"}}}
    wins = {}
    for p in passages:
        for n in re.findall(r"within (\d+) days of (?:the )?([a-z ]+?)[,.]", p["text"]):
            wins.setdefault(n[1].strip(), set()).add(n[0])
    if any(len(v) > 1 for v in wins.values()) and not any(R.r_precedence(p) for p in passages) and not _did(obs, "search_corpus", purpose="precedence"):
        return {"thought": "The documents give different time limits for the same event; find the precedence provision.",
                "action": {"tool": "search_corpus", "args": {"query": "inconsistency between bye-laws and academic regulations precedence ruling", "mode": "hybrid", "top_k": 2, "doc": "BYE", "purpose": "precedence"}}}
    return {"thought": "Base provision, modifiers and Bye-Law limits are in hand.", "finish": True}


def draft(pl: dict, version: str) -> dict:
    d = R.decide(pl["fields"], pl["petition_type"], pl["passages"], style=version)
    with _flock:
        mode = FAULT["mode"] if FAULT["remaining"] > 0 else "none"
        if mode in ("hallucinate", "rogue", "bad_quote"):
            FAULT["remaining"] -= 1
    if mode == "hallucinate":
        d["citations"].append({"clause_id": "REG-9.9", "quote": "A shortfall of up to 50% shall be condoned automatically.", "role": "modifier"})
        d["rationale"] += " Under REG-9.9 shortfalls of up to 50% are condoned automatically."
    elif mode == "bad_quote" and d["citations"]:
        d["citations"][0]["quote"] = "Any shortfall in attendance shall be condoned by the Head of Department."
    elif mode == "rogue":
        d["route"] = "grant-under-provision"
        d["rationale"] = "The petition is hereby approved. This is the final decision of the Examination Section."
    return d


def _usage(body, content):
    ptxt = " ".join(m["content"] for m in body.get("messages", []))
    return {"prompt_tokens": max(1, len(ptxt) // 4), "completion_tokens": max(1, len(content) // 4)}


def complete(body: dict) -> dict:
    md = body.get("metadata", {})
    task, pl, version = md.get("task"), md.get("payload", {}), md.get("prompt_version", "v1")
    if task == "classify":
        out = classify(pl)
    elif task == "research_plan":
        out = plan(pl)
    elif task == "draft_position":
        out = draft(pl, version)
    else:
        raise ValueError(f"unknown task {task}")
    content = json.dumps(out)
    return {"id": "heuristic", "model": "heuristic-offline", "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": _usage(body, content)}
