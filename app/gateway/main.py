"""llm-gateway service: the interface we control between agents and any model provider (section 12).
 GATEWAY_PROVIDER=heuristic     deterministic offline provider with realistic latency (default; also the D6 load-test stub)
 GATEWAY_PROVIDER=openai_compat forwards to GATEWAY_UPSTREAM_URL (any OpenAI-compatible free tier) with LLM_API_KEY
Fault injection for the demonstration: POST /admin/fault {"mode": "...", "count": n}
  error (HTTP 503) | slow (sleeps past the client timeout) | ratelimit (429) | hallucinate | bad_quote | rogue | none"""
from __future__ import annotations
import asyncio, os, random, time
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest, Counter, Histogram
from app.config import settings
from app.gateway import heuristic
from app.logging_setup import get_logger
from app.telemetry import setup_tracing, span_from

os.environ.setdefault("SERVICE_NAME", "llm-gateway")
log = get_logger("gateway")
setup_tracing("llm-gateway")
app = FastAPI(title="llm-gateway")
REQS = Counter("gateway_requests_total", "Gateway requests", ["task", "outcome"])
LAT = Histogram("gateway_request_seconds", "Gateway latency", ["task"], buckets=(0.1, 0.25, 0.5, 1, 2, 4, 8, 16))
STARTED = time.time()


@app.get("/health")
def health():
    return {"status": "ok", "provider": settings.gateway_provider, "fault": heuristic.FAULT, "uptime_s": round(time.time() - STARTED)}


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/admin/fault")
async def set_fault(req: Request):
    b = await req.json()
    heuristic.FAULT.update(mode=b.get("mode", "none"), remaining=int(b.get("count", 1000000)))
    log.warning("fault injection set", extra={"fault": heuristic.FAULT})
    return heuristic.FAULT


@app.get("/admin/fault")
def get_fault():
    return heuristic.FAULT


def _take(mode):
    if heuristic.FAULT["mode"] == mode and heuristic.FAULT["remaining"] > 0:
        heuristic.FAULT["remaining"] -= 1
        return True
    return False


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    task = body.get("metadata", {}).get("task", "?")
    t0 = time.time()
    with span_from(dict(req.headers), f"gateway.{task}", task=task, provider=settings.gateway_provider):
        if _take("error"):
            REQS.labels(task, "fault_error").inc()
            return JSONResponse({"error": "injected provider failure"}, status_code=503)
        if _take("ratelimit"):
            REQS.labels(task, "fault_429").inc()
            return JSONResponse({"error": "rate limited"}, status_code=429, headers={"retry-after": "1"})
        if _take("slow"):
            await asyncio.sleep(settings.llm_timeout_s + 5)
        if settings.gateway_provider == "openai_compat":
            up = settings.gateway_upstream_url
            if not up:
                return JSONResponse({"error": "GATEWAY_UPSTREAM_URL not configured"}, status_code=500)
            fwd = {k: v for k, v in body.items() if k != "metadata"}
            fwd["model"] = fwd.get("model") or settings.llm_model
            async with httpx.AsyncClient(timeout=settings.llm_timeout_s) as c:
                r = await c.post(up.rstrip("/") + "/chat/completions", json=fwd,
                                 headers={"Authorization": f"Bearer {settings.llm_api_key}"} if settings.llm_api_key else {})
            REQS.labels(task, f"upstream_{r.status_code}").inc()
            LAT.labels(task).observe(time.time() - t0)
            return JSONResponse(r.json(), status_code=r.status_code)
        # heuristic provider: realistic latency, so load tests measure our system, not the provider (D6)
        delay = max(0, settings.gateway_latency_ms + random.uniform(-1, 1) * settings.gateway_latency_jitter_ms) / 1000
        await asyncio.sleep(delay)
        try:
            out = heuristic.complete(body)
        except Exception as e:
            REQS.labels(task, "error").inc()
            log.exception("heuristic failure")
            return JSONResponse({"error": str(e)}, status_code=500)
        REQS.labels(task, "ok").inc()
        LAT.labels(task).observe(time.time() - t0)
        return out
