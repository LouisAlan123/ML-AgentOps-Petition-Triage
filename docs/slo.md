# Service level objective (declared before any load test was run)

Declared: 2026-11-12T03:11:30Z. The load-test results in `docs/loadtest_results.md` were produced after this file was written.

| SLO | Target |
|---|---|
| **p95 end-to-end run latency** (submission to final result, measured by the client that polls) | **≤ 10 s** |
| at offered load | up to **20 concurrent users**, each submitting a new petition, waiting for its result, then thinking 1 to 3 s |
| with the model replaced by the stub | 400 ms ± 150 ms per call (realistic free-tier latency) |
| error rate (HTTP 5xx or run status `failed`) | < 1 % |

## Why 10 s (justified against section 1 of the brief)
* Petitions are a *steady stream* handled by an examination officer, not a consumer chat. The officer submits a petition
  and reads the cited position before deciding. A wait that stays under ~10 s keeps that interactive; beyond it the
  officer switches task and the triage tool stops saving time.
* The agent makes ~9 sequential model calls per run (classify, ~7 research steps, draft). At 400 ms each that is
  ~3.6 s of unavoidable model time, so 10 s leaves ~2.5x headroom for queueing at peak.
* Peak load is bursty around result declaration and the end of the attendance cycle. 20 concurrent officers or a batch
  import client is a generous upper bound for one university's examination section.
