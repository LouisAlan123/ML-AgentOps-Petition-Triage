import datetime as dt, json, os
from app.contracts import PetitionIn
from app.corpus.pipeline import run_index

BASE = dict(student_ref="SYN-000001", program="BTECH", admission_batch=2024, semester=3, course_code="CS301", submitted_on=str(dt.date.today()))


def test_pipeline_idempotent_and_partial_reembed(retriever):
    m = run_index()
    assert m["last_run"]["embedded"] == 0 and m["last_run"]["unchanged"] == m["chunks"]
    m = run_index(reembed="REG-7.4")
    assert m["last_run"]["embedded"] == 1 and not m["last_run"]["full_rebuild"]


def test_reverse_reference_finds_condonation(retriever):
    assert "REG-7.4" in [p["clause_id"] for p in retriever.citing("REG-3.1")]


def _run(retriever, **kw):
    from app.agents.runner import run_inline
    p = PetitionIn(**{**BASE, **kw}).model_dump(mode="json")
    return run_inline(p, "run_00000000000000ff", retriever)[0]


def test_hard_case_4_1(retriever):
    r = _run(retriever, declared_type="attendance", grounds="medical", classes_held=60, classes_attended=41, medical_certificate_attached=True,
             days_since_resumption=3, description="I was hospitalised with dengue and missed classes. Certificate attached.")
    assert r["route"] == "refer-to-committee"
    assert {"REG-3.1", "REG-7.4"} <= {c["clause_id"] for c in r["citations"]}
    assert r["verification"]["passed"] and r["escalation"]["required"]


def test_conflict_escalates(retriever):
    r = _run(retriever, declared_type="re_evaluation", days_since_result=6, description="Please re-evaluate my answer script.")
    assert r["route"] == "escalate-to-human" and r["escalation"]["reason_code"] == "conflict"


def test_out_of_corpus_escalates(retriever):
    r = _run(retriever, description="I want to change my branch from CSE to ECE.")
    assert r["route"] == "escalate-to-human" and r["escalation"]["reason_code"] == "out_of_corpus" and not r["citations"]


def test_hallucinated_citation_caught_then_retried(retriever):
    from app.gateway import heuristic
    heuristic.FAULT.update(mode="hallucinate", remaining=1)
    r = _run(retriever, declared_type="re_registration", grade_obtained="F", attempts_used=1, description="I failed CS301 and want to re-register.")
    heuristic.FAULT.update(mode="none", remaining=0)
    assert r["verification"]["rounds"] == 2 and r["verification"]["passed"] and "REG-9.9" not in {c["clause_id"] for c in r["citations"]}


def test_eval_set_has_20_plus_and_hard_case():
    sc = [json.loads(l) for l in open(os.path.join(os.path.dirname(__file__), "..", "eval", "scenarios.jsonl"))]
    assert len(sc) >= 20 and any("hard_case_4_1" in s["tags"] for s in sc) and all(s["supporting_clauses"] is not None for s in sc)
