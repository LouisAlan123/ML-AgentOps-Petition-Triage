"""A3: verify every answer against the passages retrieved IN THIS RUN before it is returned.
Fails => adjudicator asks the researcher to search again with a different strategy, or escalates. An unverified
answer is never returned. Checks are deterministic (no model judges the model)."""
from __future__ import annotations
import re
from app.textutil import normalize, numbers

ROUTE_SUPPORT = {
    "grant-under-provision": r"counted as|may grant|may apply|shall re-register|permit|may re-register",
    "reject-with-cited-clause": r"shall not|not eligible|only if|only once|not less than|within \d+ days|not more than|shall be rejected|only|on the basis of|certified by",
    "refer-to-committee": r"Committee|Council",
}


def _num(s: str) -> str:
    s = s.rstrip("%")
    try:
        f = float(s)
        return f"{f:g}"
    except ValueError:
        return s


def allowed_numbers(petition: dict, cited: list[dict]) -> set[str]:
    nums = set()
    for p in cited:
        nums |= {_num(n) for n in numbers(p["text"])}
    for k, v in petition.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            nums.add(_num(str(v)))
    held, att = petition.get("classes_held"), petition.get("classes_attended")
    if held and att is not None:
        nums.add(_num(str(round(100 * att / held, 1))))
        pct_in_text = {float(n.rstrip("%")) for p in cited for n in numbers(p["text"]) if n.endswith("%")}
        for q in pct_in_text:
            credit = int(q / 100 * held)
            nums.add(str(credit))
            if petition.get("duty_classes") is not None:
                c = min(petition["duty_classes"], credit)
                nums |= {str(c), _num(str(round(100 * (att + c) / held, 1)))}
    return nums


def verify(draft: dict, retrieved: dict[str, dict], petition: dict) -> dict:
    checks = []

    def chk(name, ok, detail=""):
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    route = draft.get("route")
    cites = draft.get("citations") or []
    text = f"{draft.get('basis', '')} {draft.get('rationale', '')}"
    chk("route_allowed", route in ("grant-under-provision", "refer-to-committee", "reject-with-cited-clause", "escalate-to-human"), str(route))
    if route != "escalate-to-human":
        chk("citations_present", len(cites) > 0, f"{len(cites)} citations")
    unresolved = [c.get("clause_id") for c in cites if c.get("clause_id") not in retrieved]
    chk("citations_resolve_to_retrieved", not unresolved, f"unresolved: {unresolved}" if unresolved else "all resolve")
    bad_quotes = [c["clause_id"] for c in cites if c.get("clause_id") in retrieved and
                  (len(normalize(c.get("quote", ""))) < 15 or normalize(c.get("quote", "")) not in normalize(retrieved[c["clause_id"]]["text"]))]
    chk("quotes_verbatim", not bad_quotes, f"not found verbatim: {bad_quotes}" if bad_quotes else "all verbatim")
    cited = [retrieved[c["clause_id"]] for c in cites if c.get("clause_id") in retrieved]
    cited_ids = {p["clause_id"] for p in cited}
    mentioned = set(re.findall(r"\b(?:REG|BYE)-\d{1,2}\.\d{1,2}\b", text))
    not_cited = sorted(mentioned - cited_ids)
    chk("mentioned_clauses_cited", not not_cited, f"relied on but not cited: {not_cited}" if not_cited else "ok")
    allowed = allowed_numbers(petition, cited) | {m.split("-")[1] for m in mentioned & cited_ids}
    stray = sorted({_num(n) for n in numbers(re.sub(r"\b(?:REG|BYE)-\d{1,2}\.\d{1,2}\b", "", text))} - allowed)
    chk("numbers_grounded", not stray, f"numbers not in petition or cited text: {stray}" if stray else "ok")
    missing_base = []
    for p in cited:
        for m in re.finditer(r"(?:Notwithstanding|Subject to|under) Clause (\d{1,2}\.\d{1,2})", p["text"]):
            b = f"{p['doc']}-{m.group(1)}"
            if b not in cited_ids and b in retrieved and "Notwithstanding" in m.group(0):
                missing_base.append(f"{p['clause_id']} modifies {b}")
    chk("modifier_and_base_both_cited", not missing_base, "; ".join(missing_base) or "ok")
    if route in ROUTE_SUPPORT and cited:
        chk("route_supported_by_cited_text", any(re.search(ROUTE_SUPPORT[route], p["text"]) for p in cited), route)
    if route == "refer-to-committee":
        chk("committee_named_in_citation", any(re.search(r"Committee|Council", p["text"]) for p in cited), "")
    if route == "escalate-to-human":
        chk("escalation_reason_present", bool(draft.get("escalation_reason")), str(draft.get("escalation_reason")))
    failures = [c for c in checks if not c["passed"]]
    return {"passed": not failures, "checks": checks,
            "feedback": "; ".join(f"{c['check']}: {c['detail']}" for c in failures)}
