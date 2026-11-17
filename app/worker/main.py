"""Agent workers. WORKER_ROLE=researcher | adjudicator. Each process runs WORKER_THREADS consumers on its Redis stream.
Scale with:  docker compose up --scale researcher=3   (A7).

Failure handling (A2): a message is only ACKed after its result has been written. If a worker dies mid-run the
message stays pending and another worker reclaims it after QUEUE_CLAIM_IDLE_S (XAUTOCLAIM) and redoes that agent's
task (each task is idempotent: it overwrites the dossier/result for the run). After QUEUE_MAX_DELIVERIES the run is
closed as failed with an escalation, never a confident answer. A run past RUN_DEADLINE_S is closed the same way."""
from __future__ import annotations
import json, os, random, signal, socket, threading, time, warnings
warnings.filterwarnings("ignore")
from prometheus_client import start_http_server
from app import metrics as M
from app.agents.recorder import Recorder
from app.agents.runner import cost_of
from app.config import settings
from app.logging_setup import get_logger, correlation_id
from app.queue import Consumer, ensure_groups, publish, RESEARCH, ADJUDICATE
from app.store import Store
from app.telemetry import setup_tracing, span_from, inject

ROLE = settings.worker_role
os.environ.setdefault("SERVICE_NAME", ROLE)
log = get_logger(ROLE)
setup_tracing(ROLE)
STOP = threading.Event()
NAME = f"{ROLE}-{socket.gethostname()}-{os.getpid()}"
FINAL = ("completed", "failed")


def fail_run(store: Store, run_id: str, code: str, reason: str):
    r = store.get_run(run_id) or {}
    res = {"run_id": run_id, "route": "escalate-to-human", "eligibility_position": "undetermined", "petition_type": None,
           "basis": "The run did not complete.", "rationale": f"No position is returned because the run did not complete ({reason}).", "citations": [],
           "escalation": {"required": True, "reason_code": code, "reason": reason, "path": ["Examination Officer handles this petition manually"]},
           "verification": {"passed": True, "rounds": 0, "checks": [{"check": "incomplete_run_not_answered", "passed": True, "detail": reason}]},
           "degraded": True, "decision_authority": "examination_officer", "disclaimer": "Recommendation only. The examination officer decides this petition.",
           "prompt_versions": settings.prompt_versions, "corpus_version": None, "guardrail_flags": [], "cost": cost_of(store.get_trace(run_id))}
    store.update_run(run_id, status="failed", result=res, finished_at=time.time())
    store.trace(run_id, {"agent": ROLE, "step": "run_failed", "reason_code": code, "reason": reason})
    M.runs_finished.labels("escalate-to-human", "failed").inc()
    M.degraded_runs.labels(code).inc()
    if r.get("created_at"):
        M.run_latency.observe(time.time() - r["created_at"])


def live_score(store: Store, run: dict, result: dict):
    if random.random() > settings.live_score_sample_rate:
        return
    from app.eval.metrics import score_run
    m = score_run(result, run["dossier"]["retrieved"], run["petition"])
    sample = {"ts": time.time(), "run_id": result["run_id"], "route": result["route"], **{k: m[k] for k in ("groundedness", "faithfulness", "context_relevance", "hallucination")}}
    store.add_quality_sample(sample)
    for k in ("groundedness", "faithfulness", "context_relevance", "hallucination"):
        if m[k] is not None:
            M.live_quality_hist.labels(k).observe(m[k])
    recent = store.quality_samples(50)
    for k in ("groundedness", "faithfulness", "context_relevance", "hallucination"):
        xs = [x[k] for x in recent if x.get(k) is not None]
        if xs:
            M.live_quality.labels(k).set(sum(xs) / len(xs))


def handle_research(store, retriever, f: dict):
    from app.agents.researcher import research
    run_id = f["run_id"]
    run = store.get_run(run_id)
    store.update_run(run_id, status="researching")
    rec = Recorder(run_id, store)
    deadline = run["created_at"] + settings.run_deadline_s
    prior = run.get("dossier") if f.get("strategy") == "alt" else None
    dossier = research(run["petition"], run_id, retriever, rec, f.get("strategy", "primary"), f.get("feedback", ""), prior=prior, deadline=deadline)
    store.update_run(run_id, dossier=dossier, status="adjudicating", classification=dossier.get("classification") or {})
    publish(store.r, ADJUDICATE, {"run_id": run_id, "round": f.get("round", 1), "carrier": json.dumps(inject())})


def handle_adjudicate(store, _r, f: dict):
    from app.agents.adjudicator import adjudicate
    run_id, rnd = f["run_id"], int(f.get("round", 1))
    run = store.get_run(run_id)
    rec = Recorder(run_id, store)
    flags = json.loads(run.get("guardrail_flags") or "[]")
    out = adjudicate(run["petition"], run_id, run["dossier"], rnd, rec, deadline=run["created_at"] + settings.run_deadline_s, flags=flags)
    if "handoff" in out:
        store.update_run(run_id, status="researching", verify_feedback=out["handoff"])
        publish(store.r, RESEARCH, {"run_id": run_id, "strategy": out["handoff"]["strategy"], "feedback": out["handoff"]["feedback"],
                                    "round": rnd + 1, "carrier": json.dumps(inject())})
        return
    res = out["result"]
    res["cost"] = cost_of(store.get_trace(run_id))
    now = time.time()
    store.update_run(run_id, status="completed", result=res, finished_at=now)
    M.runs_finished.labels(res["route"], "degraded" if res["degraded"] else "ok").inc()
    M.run_latency.observe(now - run["created_at"])
    cacheable = res["verification"]["passed"] and not res["degraded"] and res["escalation"]["reason_code"] not in ("unverifiable", "guardrail", "failed")
    if settings.run_cache_enabled and cacheable and run.get("cache_key"):
        store.cache_set(run["cache_key"], res, settings.run_cache_ttl_s)
    try:
        live_score(store, run, res)
    except Exception:
        log.exception("live scoring failed")


def loop(store: Store, retriever, idx: int):
    stream = RESEARCH if ROLE == "researcher" else ADJUDICATE
    handler = handle_research if ROLE == "researcher" else handle_adjudicate
    c = Consumer(store.r, stream, f"{NAME}-t{idx}")
    last_claim = 0.0
    while not STOP.is_set():
        try:
            msgs = []
            if time.time() - last_claim > 5:
                last_claim = time.time()
                msgs = c.reclaim(int(settings.claim_idle_s * 1000))
                for _ in msgs:
                    M.redeliveries.labels(stream).inc()
            msgs = msgs or c.read(block_ms=1000)
            for mid, f in msgs:
                run_id = f.get("run_id", "?")
                correlation_id.set(run_id)
                run = store.get_run(run_id)
                if not run or run["status"] in FINAL:
                    c.ack(mid); continue
                n = c.deliveries(mid)
                if n > settings.max_deliveries:
                    fail_run(store, run_id, "failed", f"{ROLE} failed {n - 1} times"); c.ack(mid); continue
                if time.time() - run["created_at"] > settings.run_deadline_s:
                    fail_run(store, run_id, "failed", f"run deadline of {settings.run_deadline_s:.0f}s exceeded"); c.ack(mid); continue
                if n > 1:
                    store.trace(run_id, {"agent": ROLE, "step": "redelivered", "delivery": n, "consumer": c.name})
                with span_from(json.loads(f.get("carrier") or "{}"), f"worker.{ROLE}", run_id=run_id, delivery=n):
                    try:
                        handler(store, retriever, f)
                        c.ack(mid)
                    except Exception as e:  # leave un-acked: another worker reclaims it
                        log.exception("task failed; will be redelivered", extra={"err": str(e)})
                        M.agent_tasks.labels(ROLE, "error").inc()
                        store.trace(run_id, {"agent": ROLE, "step": "task_error", "error": f"{type(e).__name__}: {e}", "delivery": n})
            M.queue_depth.labels(stream).set(c.depth())
        except Exception:
            log.exception("worker loop error")
            time.sleep(1)


def main():
    start_http_server(settings.metrics_port)
    store = Store(settings.redis_url)
    while not store.ping():
        log.warning("waiting for redis"); time.sleep(1)
    ensure_groups(store.r)
    retriever = None
    if ROLE == "researcher":
        from app.corpus.retriever import Retriever
        retriever = Retriever(settings.index_dir, settings.embedding_backend, settings.embedding_model)
        m = retriever.meta
        import datetime as dt
        M.index_info.info({"corpus_version": m["corpus_version"], "embedding_model": m["embedding_model"], "built_at": m["built_at"]})
        M.index_chunks.set(m["chunks"]); M.index_built_ts.set(dt.datetime.fromisoformat(m["built_at"]).timestamp())
        M.index_throughput.set(m["throughput"]["chunks_per_s"])
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    ts = [threading.Thread(target=loop, args=(store, retriever, i), daemon=True) for i in range(settings.worker_threads)]
    for t in ts:
        t.start()
    log.info("worker started", extra={"role": ROLE, "threads": settings.worker_threads})
    while not STOP.is_set():
        time.sleep(0.5)
    log.info("worker stopping")


if __name__ == "__main__":
    main()
