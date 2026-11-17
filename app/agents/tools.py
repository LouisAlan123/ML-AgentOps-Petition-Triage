"""A1: tools the researcher agent CHOOSES to call. Each call is allow-listed and argument-validated (L4), bounded by
the run's tool-call budget and a per-call timeout, recorded in the trace and counted in metrics."""
from __future__ import annotations
import concurrent.futures as cf
import time
from app import metrics as M
from app.guardrails import validate_tool_call
from app.telemetry import span

_pool = cf.ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool")


class BudgetExceeded(Exception):
    pass


class Toolbox:
    def __init__(self, retriever, petition: dict, recorder, max_calls: int, timeout_s: float, agent="researcher"):
        self.r, self.petition, self.rec, self.max_calls, self.timeout_s, self.agent = retriever, petition, recorder, max_calls, timeout_s, agent
        self.calls = 0

    # ---------------- the tools ----------------
    def search_corpus(self, query: str, mode: str = "hybrid", top_k: int = 4, doc: str | None = None, topic: str | None = None, purpose: str | None = None):
        return {"passages": self.r.search(query, mode=mode, top_k=int(top_k), doc=doc, topic=topic)}

    def get_clause(self, clause_id: str):
        p = self.r.get(clause_id)
        return {"passages": [p] if p else [], "error": None if p else f"no clause {clause_id}"}

    def get_petition_fields(self):
        f = {k: v for k, v in self.petition.items() if k not in ("description", "student_ref", "client_petition_ref")}
        return {"fields": f}

    def find_citing_clauses(self, clause_id: str):
        return {"passages": self.r.citing(clause_id)}

    def list_clauses_by_topic(self, topic: str):
        return {"listing": self.r.list_topic(topic)}

    # ---------------- dispatcher ----------------
    def call(self, name: str, args: dict) -> dict:
        ok, why = validate_tool_call(name, args)
        if not ok:
            M.tool_calls.labels(name, "rejected").inc()
            obs = {"tool": name, "args": args, "error": f"tool call rejected: {why}", "passages": []}
            self.rec.event(self.agent, "tool_call", tool=name, args=args, outcome="rejected", error=why, latency_ms=0)
            return obs
        if self.calls >= self.max_calls:
            raise BudgetExceeded("tool-call budget exhausted")
        self.calls += 1
        t0 = time.time()
        with span(f"tool.{name}", tool=name, args=str(args)[:200]):
            fut = _pool.submit(getattr(self, name), **args)
            try:
                res = fut.result(timeout=self.timeout_s)
                outcome = "ok"
            except cf.TimeoutError:
                res, outcome = {"passages": [], "error": f"timeout after {self.timeout_s}s"}, "timeout"
            except Exception as e:  # tool failure is an observation, not a crash
                res, outcome = {"passages": [], "error": f"{type(e).__name__}: {e}"}, "error"
        lat = (time.time() - t0) * 1000
        M.tool_calls.labels(name, outcome).inc()
        obs = {"tool": name, "args": args, **res}
        self.rec.event(self.agent, "tool_call", tool=name, args=args, outcome=outcome, latency_ms=round(lat, 2),
                       passages=[{"clause_id": p["clause_id"], "score": p.get("score"), "rank": p.get("rank"), "text": p["text"]} for p in res.get("passages", [])],
                       listing=res.get("listing"), error=res.get("error"))
        return obs
