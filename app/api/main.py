"""api service (A4, D1, D7, F1, F8, L6). Accepts petitions over a documented REST interface, validates them at the
boundary, quarantines rejects, applies guardrails, rate limits, whole-run cache and idempotency, then enqueues the run
and returns a run id immediately (202). Callers poll GET /v1/runs/{id}. Serves the web client at /."""
from __future__ import annotations
import json, os, secrets, time
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from pydantic import ValidationError
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from app import metrics as M
from app.config import settings
from app.contracts import PetitionIn, Feedback, RunAccepted, flatten_errors, CONTRACT_VERSION, RunResult
from app.guardrails import scan_fields
from app.logging_setup import get_logger, correlation_id
from app.prompts import manifest
from app.queue import ensure_groups, publish, RESEARCH, Consumer, ADJUDICATE
from app.store import Store, cache_key
from app.telemetry import setup_tracing, span, inject

os.environ.setdefault("SERVICE_NAME", "api")
log = get_logger("api")
setup_tracing("api")
app = FastAPI(title="Academic Petition Triage API", version="1.0",
              description="Submit a synthetic student petition; receive a cited eligibility recommendation. The system never decides petitions.")
store: Store | None = None
STARTED = time.time()
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "web")


def index_meta() -> dict:
    try:
        return json.load(open(os.path.join(settings.index_dir, "index_meta.json")))
    except Exception:
        return {}


@app.on_event("startup")
def startup():
    global store
    store = Store(settings.redis_url)
    for _ in range(30):
        if store.ping():
            break
        time.sleep(1)
    ensure_groups(store.r)
    m = index_meta()
    if m:
        M.index_info.info({"corpus_version": m["corpus_version"], "embedding_model": m["embedding_model"], "built_at": m["built_at"]})
        M.index_chunks.set(m["chunks"])
        import datetime as dt
        M.index_built_ts.set(dt.datetime.fromisoformat(m["built_at"]).timestamp())
        M.index_throughput.set(m["throughput"]["chunks_per_s"])


@app.middleware("http")
async def observe(request: Request, call_next):
    rid = request.headers.get("x-request-id") or secrets.token_hex(8)
    correlation_id.set(rid)
    t0 = time.time()
    route = request.scope.get("route").path if request.scope.get("route") else request.url.path
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("unhandled")
        resp = JSONResponse({"error": "internal error"}, status_code=500)
    route = request.scope.get("route").path if request.scope.get("route") else "unmatched"
    M.http_requests.labels("api", route, request.method, str(resp.status_code)).inc()
    M.http_latency.labels("api", route).observe(time.time() - t0)
    resp.headers["x-request-id"] = rid
    return resp


def _client(request: Request) -> str:
    c = request.headers.get("x-client-id", "anonymous")
    return c if c.replace("-", "").replace("_", "").isalnum() and len(c) <= 40 else "invalid"


def _guard_result(run_id, flags, ptype=None):
    return {"run_id": run_id, "route": "escalate-to-human", "eligibility_position": "undetermined", "petition_type": ptype,
            "basis": "Input blocked by guardrails before reaching any model.",
            "rationale": "The free-text description was flagged by the input guardrail, so it was not sent to any model and no position was formed.",
            "citations": [], "escalation": {"required": True, "reason_code": "guardrail", "reason": "Possible prompt injection or abusive content in the petition text.",
                                            "path": ["Examination Officer reviews the petition manually"]},
            "verification": {"passed": True, "rounds": 0, "checks": [{"check": "no_model_output", "passed": True, "detail": "blocked before any agent ran"}]},
            "degraded": False, "decision_authority": "examination_officer", "disclaimer": "Recommendation only. The examination officer decides this petition.",
            "prompt_versions": settings.prompt_versions, "corpus_version": index_meta().get("corpus_version"), "guardrail_flags": flags, "cost": {"cost_usd": 0, "llm_calls": 0}}


@app.post("/v1/petitions", status_code=202, response_model=RunAccepted, responses={422: {"description": "rejected and quarantined"}, 429: {"description": "rate limited"}})
async def submit(request: Request):
    client = _client(request)
    ok, retry = store.rate_check(client, settings.rate_limit_per_min, settings.rate_limit_burst_runs_per_hour)
    if not ok:
        M.rate_limited.labels(client).inc()
        return JSONResponse({"error": "rate limit exceeded", "retry_after_s": retry}, status_code=429, headers={"retry-after": str(retry)})
    with span("api.submit", client=client) as sp:
        try:
            raw = await request.json()
        except Exception:
            qid = store.quarantine((await request.body()).decode(errors="replace")[:4000], [{"field": "body", "error": "not valid JSON"}], client, "malformed_json")
            M.runs_rejected.labels("malformed_json").inc()
            return JSONResponse({"error": "rejected", "quarantine_id": qid, "errors": [{"field": "body", "error": "not valid JSON"}], "run_started": False}, status_code=422)
        try:
            pet = PetitionIn(**raw) if isinstance(raw, dict) else PetitionIn.model_validate(raw)
        except ValidationError as e:
            errs = flatten_errors(e)
            qid = store.quarantine(raw, errs, client, "contract_violation")
            M.runs_rejected.labels("contract_violation").inc()
            log.info("request quarantined", extra={"quarantine_id": qid, "errors": errs})
            return JSONResponse({"error": "rejected", "contract": CONTRACT_VERSION, "quarantine_id": qid, "errors": errs, "run_started": False}, status_code=422)
        p = pet.model_dump(mode="json")
        run_id = "run_" + secrets.token_hex(8)
        sp.set_attribute("run_id", run_id)
        correlation_id.set(run_id)
        if p.get("client_petition_ref"):
            prev = store.idem_get_or_set(client, p["client_petition_ref"], run_id)
            if prev:
                return RunAccepted(run_id=prev, status=(store.get_run(prev) or {}).get("status", "unknown"), status_url=f"/v1/runs/{prev}")
        g = scan_fields(raw)
        for f in g.flags:
            M.guardrail_events.labels("input", f).inc()
        if g.blocked:
            store.create_run(run_id, p, client, {"status": "completed", "guardrail": json.dumps(g.__dict__)})
            res = _guard_result(run_id, g.flags)
            store.update_run(run_id, result=res, status="completed", finished_at=time.time())
            store.trace(run_id, {"agent": "api", "step": "input_guard", "blocked": True, "flags": g.flags, "hits": g.injection_hits + g.toxicity_hits,
                                 "note": "no agent run started; text never reached a model"})
            M.runs_finished.labels("escalate-to-human", "blocked").inc()
            log.warning("input blocked by guardrail", extra={"flags": g.flags})
            return RunAccepted(run_id=run_id, status="completed", status_url=f"/v1/runs/{run_id}")
        meta = index_meta()
        ck = cache_key(p, meta.get("corpus_version", ""), settings.prompt_versions, settings.search_strategy)
        if settings.run_cache_enabled:
            hit = store.cache_get(ck)
            if hit:
                M.cache_events.labels("hit").inc()
                store.create_run(run_id, p, client, {"status": "completed", "cache_key": ck})
                hit = {**hit, "run_id": run_id, "cached_from": hit["run_id"], "cost": {"cost_usd": 0, "llm_calls": 0, "note": "served from whole-run cache"}}
                store.update_run(run_id, result=hit, status="completed", finished_at=time.time())
                store.trace(run_id, {"agent": "api", "step": "cache_hit", "cached_from": hit["cached_from"], "cache_key": ck})
                M.runs_finished.labels(hit["route"], "cached").inc()
                return RunAccepted(run_id=run_id, status="completed", status_url=f"/v1/runs/{run_id}", cached=True)
            M.cache_events.labels("miss").inc()
        store.create_run(run_id, p, client, {"cache_key": ck, "guardrail_flags": json.dumps(g.flags)})
        store.trace(run_id, {"agent": "api", "step": "accepted", "client": client, "guardrail_flags": g.flags, "contract": CONTRACT_VERSION})
        publish(store.r, RESEARCH, {"run_id": run_id, "strategy": "primary", "round": 1, "feedback": "", "carrier": json.dumps(inject())})
        M.runs_submitted.inc()
        return RunAccepted(run_id=run_id, status="queued", status_url=f"/v1/runs/{run_id}")


@app.get("/v1/runs/{run_id}")
def get_run(run_id: str):
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404, "unknown run")
    out = {"run_id": run_id, "status": r["status"], "created_at": r["created_at"], "updated_at": r["updated_at"]}
    if r.get("finished_at"):
        out["e2e_seconds"] = round(r["finished_at"] - r["created_at"], 3)
    if "result" in r:
        out["result"] = r["result"]
    return out


@app.get("/v1/runs/{run_id}/trace")
def get_trace(run_id: str):
    """Reconstruct the entire run: every tool call, query, passage, prompt version, per-step latency and cost, verification."""
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404, "unknown run")
    ev = store.get_trace(run_id)
    steps = [{"agent": e["agent"], "step": e["step"], "latency_ms": e.get("latency_ms"), "tool": e.get("tool"), "args": e.get("args"),
              "prompt": f"{e['prompt']}.{e['prompt_version']}#{e['prompt_sha']}" if e.get("prompt") else None,
              "tokens": (e.get("prompt_tokens", 0) + e.get("completion_tokens", 0)) or None, "cost_usd": e.get("cost_usd"),
              "passages": [p["clause_id"] for p in e.get("passages") or []] or None, "outcome": e.get("outcome") or e.get("passed")} for e in ev]
    return {"run_id": run_id, "status": r["status"], "petition": r["petition"], "result": r.get("result"),
            "otel_trace_ids": sorted({e.get("otel_trace_id") for e in ev if e.get("otel_trace_id")}),
            "summary": steps, "events": ev}


@app.post("/v1/feedback")
async def feedback(request: Request):
    try:
        fb = Feedback(**(await request.json()))
    except ValidationError as e:
        return JSONResponse({"error": "rejected", "errors": flatten_errors(e)}, status_code=422)
    r = store.get_run(fb.run_id)
    if not r:
        raise HTTPException(404, "unknown run")
    d = fb.model_dump()
    d["route"] = (r.get("result") or {}).get("route")
    store.add_feedback(d)
    M.feedback_total.labels(str(fb.satisfactory).lower()).inc()
    return {"ok": True}


@app.get("/v1/feedback/summary")
def feedback_summary():
    return store.feedback_summary()


@app.get("/v1/quality")
def quality():
    s = store.quality_samples()
    keys = ("groundedness", "faithfulness", "context_relevance", "hallucination")
    agg = {k: (round(sum(x[k] for x in s if x.get(k) is not None) / max(1, sum(1 for x in s if x.get(k) is not None)), 3)) for k in keys}
    return {"samples": len(s), "mean": agg, "series": s[-100:]}


@app.get("/v1/quarantine")
def quarantine(n: int = 20):
    return store.list_quarantine(n)


@app.get("/v1/contract")
def contract():
    return {"version": CONTRACT_VERSION, "request": PetitionIn.model_json_schema(), "result": RunResult.model_json_schema(), "feedback": Feedback.model_json_schema()}


@app.get("/v1/prompts")
def prompts():
    return {"active": settings.prompt_versions, "available": manifest()}


@app.get("/health")
def health():
    return {"status": "ok", "uptime_s": round(time.time() - STARTED)}


@app.get("/ready")
def ready():
    m = index_meta()
    ok = store.ping() and bool(m)
    body = {"redis": store.ping(), "index": bool(m), "index_built_at": m.get("built_at"), "chunks": m.get("chunks"),
            "queue_depth": {RESEARCH: Consumer(store.r, RESEARCH, "api").depth(), ADJUDICATE: Consumer(store.r, ADJUDICATE, "api").depth()}}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/metrics")
def metrics():
    for s in (RESEARCH, ADJUDICATE):
        M.queue_depth.labels(s).set(Consumer(store.r, s, "api").depth())
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
def web():
    return FileResponse(os.path.join(WEB, "index.html"))
