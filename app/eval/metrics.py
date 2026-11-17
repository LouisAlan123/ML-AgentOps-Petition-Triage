"""Quality metrics (L3), computed without an LLM judge so they are deterministic and free ("TruLens or equivalent").

Claims = sentences of the result's rationale. A claim is SUPPORTED by a set of texts when
  (a) >= 60% of its content tokens (stemmed, stop-words, routing words and clause ids removed) occur in those texts
      or in the petition's own field names/values, and
  (b) every number in it occurs in those texts or in the petition's fields.

 groundedness       = mean over claims of supported(claim, ALL passages retrieved in the run)
 faithfulness       = mean over claims of supported(claim, ONLY the passages the answer CITES)
                      (an answer can be grounded in what was retrieved yet unfaithful to what it cites)
 context_relevance  = fraction of retrieved passages whose topic tags match the petition type's topics
 hallucination      = 1 if the run has any unsupported claim, an unresolved citation, or a non-verbatim quote, else 0
                      (hallucination_rate = mean over runs)
Abstentions (escalations with no claims about the regulations: out-of-corpus, degraded) are excluded from
groundedness/faithfulness and count as non-hallucinating."""
from __future__ import annotations
import re
from app.textutil import tokens, sentences, numbers, normalize, ROUTING_WORDS

TYPE_TOPICS = {"attendance": {"attendance", "condonation"}, "re_evaluation": {"re_evaluation"}, "re_registration": {"re_registration"},
               "medical_leave": {"medical_leave"}, "grade_grievance": {"grievance"}}
THRESHOLD = 0.6


def _content(s: str) -> list[str]:
    s = re.sub(r"\b(?:REG|BYE)-\d{1,2}\.\d{1,2}\b", "", s)
    return [t for t in tokens(s) if t not in ROUTING_WORDS and not re.fullmatch(r"\d+(\.\d+)?%?", t)]


def _petition_vocab(petition: dict) -> tuple[set, set]:
    txt = " ".join(f"{k} {v}" for k, v in petition.items() if k != "description")
    nums = {n.rstrip("%") for n in numbers(txt)}
    if petition.get("classes_held") and petition.get("classes_attended") is not None:
        nums.add(f"{round(100 * petition['classes_attended'] / petition['classes_held'], 1):g}")
    return set(tokens(txt)), nums


def supported(claim: str, texts: list[str], petition: dict) -> bool:
    from app.agents.verifier import allowed_numbers
    vocab, pnums = _petition_vocab(petition)
    pnums |= allowed_numbers(petition, [{"text": t} for t in texts])
    tv = set()
    tn = set()
    for t in texts:
        tv |= set(tokens(t))
        tn |= {n.rstrip("%") for n in numbers(t)}
    ct = _content(claim)
    cov = sum(1 for t in ct if t in tv or t in vocab) / len(ct) if ct else 1.0
    cn = {n.rstrip("%") for n in numbers(re.sub(r"\b(?:REG|BYE)-\d{1,2}\.\d{1,2}\b", "", claim))}
    cn = {f"{float(n):g}" for n in cn}
    okn = {f"{float(n):g}" for n in (tn | pnums)}
    return cov >= THRESHOLD and cn <= okn


def score_run(result: dict, retrieved: dict, petition: dict) -> dict:
    claims = sentences(result.get("rationale", ""))
    cited_texts = [retrieved[c["clause_id"]]["text"] for c in result.get("citations", []) if c["clause_id"] in retrieved]
    all_texts = [p["text"] for p in retrieved.values()]
    abstain = result["route"] == "escalate-to-human" and result["escalation"]["reason_code"] in ("out_of_corpus", "degraded", "guardrail", "unverifiable", "insufficient_evidence")
    topics = TYPE_TOPICS.get(result.get("petition_type") or "", set())
    rel = [bool(set(p.get("topics", [])) & topics) for p in retrieved.values()]
    out = {"context_relevance": round(sum(rel) / len(rel), 3) if rel and topics else None, "abstained": abstain}
    if abstain or not claims:
        out.update(groundedness=None, faithfulness=None, hallucination=0, unsupported_claims=[])
        return out
    g = [supported(c, all_texts, petition) for c in claims]
    f = [supported(c, cited_texts, petition) for c in claims]
    bad_cite = any(c["clause_id"] not in retrieved or normalize(c["quote"]) not in normalize(retrieved[c["clause_id"]]["text"])
                   for c in result.get("citations", []))
    out.update(groundedness=round(sum(g) / len(g), 3), faithfulness=round(sum(f) / len(f), 3),
               hallucination=int((not all(g)) or bad_cite),
               unsupported_claims=[c for c, ok in zip(claims, g) if not ok])
    return out
