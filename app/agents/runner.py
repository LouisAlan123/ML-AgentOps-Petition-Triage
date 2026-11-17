"""Orchestration shared by the workers and the in-process evaluation harness."""
from __future__ import annotations
import time
from app.agents.researcher import research
from app.agents.adjudicator import adjudicate
from app.agents.recorder import Recorder
from app.config import settings


def cost_of(events: list[dict]) -> dict:
    by_step: dict = {}
    tot = {"prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0, "llm_calls": 0}
    for e in events:
        if "prompt_tokens" in e:
            k = f"{e['agent']}.{e['step']}"
            b = by_step.setdefault(k, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
            b["calls"] += 1; tot["llm_calls"] += 1
            for f in ("prompt_tokens", "completion_tokens", "cost_usd"):
                b[f] += e[f]; tot[f] += e[f]
    tot["cost_usd"] = round(tot["cost_usd"], 8)
    return {**tot, "by_step": by_step}


def run_inline(petition: dict, run_id: str, retriever, store=None, strategy_mode: str | None = None) -> tuple[dict, list]:
    rec = Recorder(run_id, store)
    deadline = time.time() + settings.run_deadline_s
    dossier, rnd, strategy, feedback = None, 1, "primary", ""
    while True:
        dossier = research(petition, run_id, retriever, rec, strategy, feedback, prior=dossier, deadline=deadline, strategy_mode=strategy_mode)
        out = adjudicate(petition, run_id, dossier, rnd, rec, deadline=deadline)
        if "handoff" in out:
            rnd, strategy, feedback = rnd + 1, out["handoff"]["strategy"], out["handoff"]["feedback"]
            continue
        res = out["result"]
        res["cost"] = cost_of(rec.events)
        return res, rec.events
