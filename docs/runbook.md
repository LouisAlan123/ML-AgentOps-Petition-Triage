# Runbook: the two most likely failure modes

## 1. Model provider unavailable / rate-limited (circuit breaker open)
**Symptom:** runs complete quickly but every result is `escalate-to-human` with `escalation.reason_code = degraded` and
`degraded: true`. The Grafana "Circuit breaker open" stat is 1; "LLM calls by outcome" shows `breaker_open` / `failed`.

**Confirm:**
```
curl -s localhost:8080/health                                   # gateway up? fault mode?
docker compose logs researcher | grep -E "circuit breaker|llm retry" | tail
curl -s localhost:8000/v1/runs/<run_id>/trace | jq '.events[] | select(.fallback)'
```
A free-tier 429 shows as `outcome="failed"` after retries, with `retry-after` honoured in the logs.

**Recover:**
1. Provider outage or rate limit: wait. The breaker half-opens after `BREAKER_OPEN_S` (15 s), and one successful
   probe closes it. No restart is needed.
2. Bad key or endpoint (HTTP 401/404 in gateway logs): fix `LLM_API_KEY` / `GATEWAY_UPSTREAM_URL` in `.env`, then run
   `docker compose up -d llm-gateway`.
3. Sustained free-tier limits: switch `GATEWAY_PROVIDER=heuristic` to keep the demo running, or lower
   `RATE_LIMIT_PER_MIN` so our own clients cannot exhaust the provider.
4. Degraded results are never cached, so nothing needs purging after recovery. Re-submit affected petitions
   (find them with `scripts/cost_report.py` or by searching the logs for `reason_code=degraded`).

## 2. Research backlog: runs stuck in `queued`/`researching`, p95 rising
**Symptom:** p95 climbs past 10 s; the "Queue depth" panel shows `stream:research` growing; runs stay in `researching`.

**Confirm:**
```
curl -s localhost:8000/ready | jq .queue_depth          # research backlog vs adjudicate backlog
docker compose ps researcher                             # are replicas healthy / restarting?
docker compose logs researcher | grep -E "task failed|worker loop error" | tail
```
* A backlog only on `stream:research` with low CPU means capacity: researcher slots are blocked on model calls.
* A worker restarting in a loop means a crash: check logs. A dead worker's messages are re-claimed by others
  after `QUEUE_CLAIM_IDLE_S`, visible as `redelivered` events in the run trace.

**Recover:**
1. Capacity: `docker compose up -d --scale researcher=3` (or raise `WORKER_THREADS`). Scaling the api or adjudicator does not help.
2. Crash loop: fix the cause and redeploy that service only (`docker compose up -d --build researcher`). In-flight runs
   are re-claimed. Runs that exceed `QUEUE_MAX_DELIVERIES` or `RUN_DEADLINE_S` are closed as `failed` with an
   escalation, never as an answer.
3. Index missing or corrupt (researcher logs "index built with … but runtime embedder is …"):
   `docker compose run --rm indexer`, then `docker compose restart researcher`.
