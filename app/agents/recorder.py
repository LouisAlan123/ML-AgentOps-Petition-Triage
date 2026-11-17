"""Per-run trace recorder: every tool call, query, passage, prompt version, per-step latency, tokens, cost and
verification outcome is appended here, so GET /v1/runs/{id}/trace reconstructs the whole run (D5)."""
from __future__ import annotations
import time
from app import metrics as M
from app.logging_setup import get_logger
from app.telemetry import current_trace_id

log = get_logger("trace")


class Recorder:
    def __init__(self, run_id: str, store=None):
        self.run_id, self.store, self.events = run_id, store, []

    def event(self, agent: str, step: str, **data):
        ev = {"run_id": self.run_id, "agent": agent, "step": step, "ts": time.time(), "otel_trace_id": current_trace_id(), **data}
        self.events.append(ev)
        if self.store:
            self.store.trace(self.run_id, ev)
        if "latency_ms" in data:
            M.step_latency.labels(agent, step).observe(data["latency_ms"] / 1000)
        log.info(f"{agent}.{step}", extra={"extra_fields": {k: v for k, v in ev.items() if k in ("agent", "step", "tool", "latency_ms", "outcome", "cost_usd", "prompt_version")}})
        return ev
