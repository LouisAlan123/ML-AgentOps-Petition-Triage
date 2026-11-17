"""Runs Locust headless at several concurrencies and summarises run_e2e RPS / p50 / p95 against the SLO.
  python loadtest/run_matrix.py --label researchers1 --users 5,10,20,40 --duration 45"""
import argparse, csv, json, os, subprocess, sys
ap = argparse.ArgumentParser()
ap.add_argument("--label", required=True); ap.add_argument("--users", default="5,10,20,40"); ap.add_argument("--duration", default="45")
ap.add_argument("--host", default="http://localhost:8000"); ap.add_argument("--slo-ms", type=float, default=10000)
a = ap.parse_args()
os.makedirs("loadtest/out", exist_ok=True)
rows = []
for u in a.users.split(","):
    pre = f"loadtest/out/{a.label}_u{u}"
    subprocess.run(["redis-cli", "-u", os.environ.get("LT_REDIS", "redis://localhost:6379/0"), "eval",
                    "for _,k in ipairs(redis.call('keys','cache:*')) do redis.call('del',k) end return 1", "0"], capture_output=True)
    subprocess.run([sys.executable, "-m", "locust", "-f", "loadtest/locustfile.py", "--headless", "-u", u, "-r", str(max(1, int(u) // 2)),
                    "-t", f"{a.duration}s", "--host", a.host, "--csv", pre, "--only-summary"], check=False, capture_output=True)
    for r in csv.DictReader(open(pre + "_stats.csv")):
        if r["Name"] == "run_e2e":
            rows.append({"label": a.label, "users": int(u), "runs": int(r["Request Count"]), "failures": int(r["Failure Count"]),
                         "rps": round(float(r["Requests/s"]), 2), "p50_ms": float(r["50%"]), "p95_ms": float(r["95%"]),
                         "slo_met": float(r["95%"]) <= a.slo_ms})
    print(rows[-1] if rows else "no data", flush=True)
json.dump(rows, open(f"loadtest/out/{a.label}.json", "w"), indent=1)
