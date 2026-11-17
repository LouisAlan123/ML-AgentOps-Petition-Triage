# Load test results (D6, A7, D7)

SLO declared beforehand in [slo.md](slo.md): **p95 end-to-end ≤ 10 s at up to 20 concurrent users**, stub model at
400 ± 150 ms per call. Measured with `loadtest/run_matrix.py` (Locust, headless, 40 s per level, every petition unique
so every run executes the full agent loop). Metric = `run_e2e`: submission → polling client sees the final result.

**Where it was measured:** the services ran as separate native processes on a 2-vCPU / 8 GB sandbox
(`scripts/dev_up.sh N`), because the Docker daemon was not available there. `docker compose up --scale researcher=3`
starts the same processes in containers. **Re-run the matrix under Docker Compose on your machine before the
demonstration** (README → "Load test") and put those numbers in your report.

## Results

| researchers (×4 threads) | users | runs completed | RPS | p50 | p95 | SLO (p95 ≤ 10 s) |
|---|---|---|---|---|---|---|
| 1 | 5  | 29  | 0.74 | 4.3 s  | 5.3 s  | met |
| 1 | 10 | 41  | 1.05 | 7.1 s  | 8.7 s  | met |
| 1 | 20 | 36  | 0.92 | 16.0 s | 22.0 s | **failed** |
| 1 | 40 | 26  | 0.67 | 28.0 s | 37.0 s | **failed** |
| 3 | 5  | 29  | 0.74 | 4.6 s  | 5.1 s  | met |
| 3 | 10 | 61  | 1.56 | 4.1 s  | 4.8 s  | met |
| 3 | 20 | 115 | 2.95 | 4.3 s  | 6.5 s  | met |
| 3 | 40 | 111 | 2.84 | 10.0 s | 14.0 s | **failed** |

Zero failed runs at every level. At saturation Locust only counts runs that finished inside the 40 s window, so the
saturated rows *understate* both RPS and p95 (queued runs never get counted). The direction of the conclusion is not affected.

## Where the target fails, and why
* **1 researcher: target fails between 10 and 20 users.** Throughput flattens at ~1 run/s.
* **3 researchers: target met at 20 users, fails between 20 and 40.** Throughput flattens at ~2.9 runs/s.
* **Component responsible: the researcher pool.** During the 40-user run the research stream held ~30 waiting messages
  while the adjudicate stream held 0-4, and every process was below 25 % CPU. Each research task makes ~8 *sequential*
  model calls (~3.6 s), so each researcher thread completes ~0.28 tasks/s; 12 threads ≈ 3.3 runs/s, matching the
  observed 2.9. The bottleneck is concurrency slots blocked on model I/O, not CPU, the API, Redis or the adjudicator.
* **Scaling helped** (3.2× throughput at 20 users, p95 22 s → 6.5 s) because the bottleneck is exactly the scaled
  service. Next levers, in order: more researcher replicas/threads (cheap: they are I/O-bound); fewer planning steps per
  run; and with a real provider, the provider's rate limit becomes the bottleneck long before ours. That is why the
  load test uses the stub.

## Caching (D7)
Whole-run cache: key = hash(petition facts excluding identity fields, corpus version, prompt versions, strategy). Only
verified, non-degraded results are cached. Measured at 20 users, 3 researchers, on a repeat-heavy workload
(`CACHE_MODE=mixed`: 5 distinct petitions submitted repeatedly):

| | runs | RPS | p50 | p95 | cost / run (reference rates) |
|---|---|---|---|---|---|
| cache off | 116 | 2.97 | 4.4 s | 5.9 s | $0.00213 |
| cache on  | 334 | 8.58 | 6 ms  | 3.9 s | $0.00012 (−94 %) |

This is a best case. For all-unique petitions the hit rate is 0 and caching changes nothing. The query-embedding LRU
cache in the retriever is in place but has negligible effect with the hashing embedder; it matters with
sentence-transformers.

**Rate limiting** (cost control): per client 60 submissions/min and 600 runs/hour (`RATE_LIMIT_*`), 429 + Retry-After.
One client looping against the agent is cut off before it can burn the budget.

## Cost (L5)
From `python scripts/cost_report.py` over 518 load-test runs (reference rates $0.15 / $0.60 per 1M prompt/completion tokens):
**$0.213 per 100 runs**. Largest single driver: **`researcher.plan` = 81 % of cost**, because each planning call
resends every observation so far (prompt grows with each step). Then `adjudicator.draft_position` 15 % and
`researcher.classify` 4 %. Cheapest fix: send the planner a summary of earlier observations instead of full passage text.
