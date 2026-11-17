"""L2/L3: batch evaluation from the command line.

  python -m app.eval.run_eval                                   # current config
  python -m app.eval.run_eval --compare-prompts v1 v2           # L2: two draft_position prompt variants
  python -m app.eval.run_eval --compare-strategy single_pass agentic   # L3: the deliberate change, before/after
Writes eval/results/<name>.json and prints a table."""
from __future__ import annotations
import argparse, json, os, statistics, sys, time, warnings
warnings.filterwarnings("ignore")
from app.contracts import PetitionIn
from app.config import settings
from app.eval.metrics import score_run


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 3) if xs else None


def evaluate(scenarios, retriever, strategy_mode=None, label="run") -> dict:
    from app.agents.runner import run_inline
    rows = []
    for i, sc in enumerate(scenarios):
        p = PetitionIn(**sc["petition"]).model_dump(mode="json")
        t0 = time.time()
        res, events = run_inline(p, f"run_{i:016x}", retriever, strategy_mode=strategy_mode)
        retrieved = {}
        for e in events:
            for ps in e.get("passages") or []:
                retrieved[ps["clause_id"]] = retriever.passage(ps["clause_id"])
        m = score_run(res, retrieved, p)
        cited = {c["clause_id"] for c in res["citations"]}
        gold = set(sc["supporting_clauses"])
        rows.append({"id": sc["id"], "title": sc["title"], "expected": sc["expected_route"], "got": res["route"],
                     "route_ok": res["route"] == sc["expected_route"],
                     "escalation_ok": (res["escalation"]["reason_code"] == sc["expected_escalation"]) if sc["expected_escalation"] else True,
                     "citation_recall": round(len(cited & gold) / len(gold), 3) if gold else None,
                     "context_recall": round(len(set(retrieved) & gold) / len(gold), 3) if gold else None,
                     "verified": res["verification"]["passed"], "verify_rounds": res["verification"]["rounds"],
                     "tool_calls": res["research"]["tool_calls"], "llm_calls": res["cost"]["llm_calls"],
                     "cost_usd": res["cost"]["cost_usd"], "latency_s": round(time.time() - t0, 3), "tags": sc["tags"],
                     "cited": sorted(cited), **m})
    summ = {"label": label, "n": len(rows), "prompt_versions": settings.prompt_versions, "strategy": strategy_mode or settings.search_strategy,
            "route_accuracy": _mean([r["route_ok"] for r in rows]),
            "escalation_accuracy": _mean([r["escalation_ok"] for r in rows]),
            "hard_case_4_1_pass": all(r["route_ok"] and r["citation_recall"] == 1 for r in rows if "hard_case_4_1" in r["tags"]),
            "groundedness": _mean([r["groundedness"] for r in rows]), "faithfulness": _mean([r["faithfulness"] for r in rows]),
            "context_relevance": _mean([r["context_relevance"] for r in rows]), "hallucination_rate": _mean([r["hallucination"] for r in rows]),
            "citation_recall": _mean([r["citation_recall"] for r in rows]), "context_recall": _mean([r["context_recall"] for r in rows]),
            "verification_retry_rate": _mean([r["verify_rounds"] > 1 for r in rows]),
            "mean_llm_calls": _mean([r["llm_calls"] for r in rows]), "cost_per_100_runs_usd": round(100 * statistics.mean(r["cost_usd"] for r in rows), 5)}
    return {"summary": summ, "rows": rows}


METRICS = ["route_accuracy", "escalation_accuracy", "hard_case_4_1_pass", "groundedness", "faithfulness", "context_relevance",
           "hallucination_rate", "citation_recall", "context_recall", "verification_retry_rate", "mean_llm_calls", "cost_per_100_runs_usd"]


def table(results: list[dict]):
    w = max(len(m) for m in METRICS) + 2
    print("metric".ljust(w) + "".join(r["summary"]["label"].rjust(18) for r in results))
    for m in METRICS:
        print(m.ljust(w) + "".join(str(r["summary"][m]).rjust(18) for r in results))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="eval/scenarios.jsonl")
    ap.add_argument("--compare-prompts", nargs=2, metavar=("A", "B"))
    ap.add_argument("--compare-strategy", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--out", default="eval/results")
    ap.add_argument("--show-failures", action="store_true")
    a = ap.parse_args()
    os.environ.setdefault("LLM_BASE_URL", "inprocess://heuristic")
    from app.corpus.retriever import Retriever
    r = Retriever(settings.index_dir, settings.embedding_backend, settings.embedding_model)
    sc = [json.loads(l) for l in open(a.scenarios)]
    os.makedirs(a.out, exist_ok=True)
    results = []
    if a.compare_prompts:
        for v in a.compare_prompts:
            os.environ["PROMPT_DRAFT_POSITION"] = v
            results.append(evaluate(sc, r, label=f"draft_{v}"))
    elif a.compare_strategy:
        for s in a.compare_strategy:
            results.append(evaluate(sc, r, strategy_mode=s, label=s))
    else:
        results.append(evaluate(sc, r, label="current"))
    for res in results:
        json.dump(res, open(os.path.join(a.out, f"{res['summary']['label']}.json"), "w"), indent=2)
    table(results)
    if a.show_failures:
        for res in results:
            for row in res["rows"]:
                if not row["route_ok"] or row["hallucination"] or (row["citation_recall"] is not None and row["citation_recall"] < 1):
                    print(f"[{res['summary']['label']}] {row['id']}: expected {row['expected']} got {row['got']} cited={row['cited']} "
                          f"recall={row['citation_recall']} unsupported={row['unsupported_claims']}")


if __name__ == "__main__":
    main()
