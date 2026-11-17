"""The only path from agents to a model. Talks to the llm-gateway service (an interface we control, so the provider
is changed by configuration). Every call: timeout, bounded retry with exponential backoff, circuit breaker,
token/cost accounting attributed to (run, agent, step), OpenTelemetry span, Prometheus metrics (A5, L5)."""
from __future__ import annotations
import json, time
import httpx
from app import metrics as M
from app.config import settings
from app.prompts import render
from app.resilience import CircuitBreaker, CircuitOpen, RetryableError, retry_call
from app.telemetry import span, inject
from app.logging_setup import get_logger

log = get_logger("llm")


class LLMUnavailable(Exception):
    """Provider unreachable / breaker open / retries exhausted. Caller must take the degradation path."""


class LLMBadOutput(Exception):
    pass


_breaker: CircuitBreaker | None = None


def breaker() -> CircuitBreaker:
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker("llm-provider", settings.breaker_threshold, settings.breaker_open_s,
                                  on_change=lambda s: (M.breaker_state.labels("llm-provider").set({"open": 1, "half_open": 0.5}.get(s, 0)),
                                                       log.warning("circuit breaker state change", extra={"state": s})))
    return _breaker


_http: httpx.Client | None = None


def _client() -> httpx.Client:
    global _http
    if _http is None:
        _http = httpx.Client(timeout=httpx.Timeout(settings.llm_timeout_s, connect=2.0),
                             limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
    return _http


def _post(body: dict) -> dict:
    base = settings.llm_base_url
    if base.startswith("inprocess://"):
        from app.gateway.heuristic import complete
        return complete(body)
    headers = inject()
    if settings.llm_api_key:
        headers["Authorization"] = f"Bearer {settings.llm_api_key}"
    try:
        resp = _client().post(base.rstrip("/") + "/v1/chat/completions", json=body, headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise RetryableError(f"transport: {type(e).__name__}")
    if resp.status_code == 429:
        ra = resp.headers.get("retry-after")
        raise RetryableError("rate limited", retry_after=min(float(ra), 5.0) if ra else None)
    if resp.status_code >= 500:
        raise RetryableError(f"provider {resp.status_code}")
    if resp.status_code >= 400:
        raise LLMBadOutput(f"provider rejected request {resp.status_code}: {resp.text[:200]}")
    return resp.json()


def chat(*, agent: str, step: str, prompt_name: str, payload: dict, variables: dict, run_id: str,
         deadline: float | None = None) -> tuple[dict, dict]:
    """Returns (parsed_json, meta). meta carries prompt version/hash, tokens, cost, latency, attempts."""
    version = settings.prompt_versions.get(prompt_name, "v1")
    text, phash = render(prompt_name, version, **variables)
    body = {"model": settings.llm_model or None, "temperature": 0, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": "Return only valid JSON."}, {"role": "user", "content": text}],
            "metadata": {"task": prompt_name, "prompt_version": version, "payload": payload, "run_id": run_id}}
    b = breaker()
    t0 = time.time()
    retries = {"n": 0}

    def on_retry(i, delay, e):
        retries["n"] = i
        M.llm_retries.labels(agent, step).inc()
        log.warning("llm retry", extra={"attempt": i, "delay_s": round(delay, 2), "err": str(e), "step": step})

    def once():
        b.before_call()        # raises CircuitOpen
        try:
            out = _post(body)
        except RetryableError:
            b.failure(); raise
        except Exception:
            b.failure(); raise
        b.success()
        return out

    with span(f"llm.{step}", agent=agent, step=step, prompt=f"{prompt_name}.{version}", run_id=run_id) as sp:
        try:
            out, attempts = retry_call(once, settings.llm_max_retries, settings.llm_backoff_base_s, deadline=deadline, on_retry=on_retry)
        except CircuitOpen as e:
            M.llm_calls.labels(agent, step, "breaker_open").inc()
            raise LLMUnavailable(str(e))
        except RetryableError as e:
            M.llm_calls.labels(agent, step, "failed").inc()
            raise LLMUnavailable(f"retries exhausted: {e}")
        except LLMBadOutput:
            M.llm_calls.labels(agent, step, "bad_output").inc()
            raise
        lat = time.time() - t0
        usage = out.get("usage", {})
        pt, ct = int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
        cost = pt / 1000 * settings.price_prompt_per_1k + ct / 1000 * settings.price_completion_per_1k
        try:
            content = out["choices"][0]["message"]["content"]
            parsed = json.loads(content)
        except Exception:
            M.llm_calls.labels(agent, step, "bad_output").inc()
            raise LLMBadOutput("model did not return JSON")
        M.llm_calls.labels(agent, step, "ok").inc()
        M.llm_latency.labels(agent, step).observe(lat)
        M.llm_tokens.labels(agent, step, "prompt").inc(pt)
        M.llm_tokens.labels(agent, step, "completion").inc(ct)
        M.llm_cost.labels(agent, step).inc(cost)
        sp.set_attribute("tokens.prompt", pt); sp.set_attribute("tokens.completion", ct); sp.set_attribute("cost_usd", cost)
        meta = {"prompt": prompt_name, "prompt_version": version, "prompt_sha": phash, "prompt_tokens": pt,
                "completion_tokens": ct, "cost_usd": round(cost, 8), "llm_latency_ms": round(lat * 1000, 1),
                "retries": attempts, "model": out.get("model", "")}
        return parsed, meta
