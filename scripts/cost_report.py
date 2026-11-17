"""L5: cost per 100 runs and the largest single cost driver, computed from the per-step records in run traces.
  python scripts/cost_report.py [--redis redis://localhost:6379/0] [--last 500]"""
import argparse, json
import redis
ap = argparse.ArgumentParser(); ap.add_argument("--redis", default="redis://localhost:6379/0"); ap.add_argument("--last", type=int, default=100000)
ap.add_argument("--json", action="store_true")
a = ap.parse_args()
r = redis.Redis.from_url(a.redis, decode_responses=True)
runs = [k for k in r.scan_iter("run:*")][: a.last]
by_step, n, total, cached, tok = {}, 0, 0.0, 0, {"prompt": 0, "completion": 0}
for k in runs:
    res = r.hget(k, "result")
    if not res:
        continue
    res = json.loads(res); n += 1
    if res.get("cached_from"):
        cached += 1
    for e in (json.loads(x) for x in r.lrange("trace:" + k[4:], 0, -1)):
        if "cost_usd" in e and "prompt_tokens" in e:
            s = by_step.setdefault(f"{e['agent']}.{e['step']}", {"calls": 0, "cost_usd": 0.0, "prompt_tokens": 0, "completion_tokens": 0})
            s["calls"] += 1; s["cost_usd"] += e["cost_usd"]; s["prompt_tokens"] += e["prompt_tokens"]; s["completion_tokens"] += e["completion_tokens"]
            total += e["cost_usd"]; tok["prompt"] += e["prompt_tokens"]; tok["completion"] += e["completion_tokens"]
out = {"runs": n, "cache_hits": cached, "total_cost_usd": round(total, 6), "cost_per_run_usd": round(total / n, 6) if n else 0,
       "cost_per_100_runs_usd": round(100 * total / n, 4) if n else 0, "tokens": tok,
       "by_step": {k: {**v, "cost_usd": round(v["cost_usd"], 6), "share": round(v["cost_usd"] / total, 3) if total else 0} for k, v in sorted(by_step.items(), key=lambda x: -x[1]["cost_usd"])}}
out["largest_cost_driver"] = next(iter(out["by_step"]), None)
print(json.dumps(out, indent=2))
