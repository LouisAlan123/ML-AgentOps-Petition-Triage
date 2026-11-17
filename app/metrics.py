"""Prometheus metrics shared by all services (A6, D2, D5, D7, L5, L6)."""
from prometheus_client import Counter, Gauge, Histogram, Info

LAT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 12, 20, 30, 60, 120)

http_requests = Counter("http_requests_total", "HTTP requests", ["service", "route", "method", "code"])
http_latency = Histogram("http_request_seconds", "HTTP latency", ["service", "route"], buckets=LAT_BUCKETS)

runs_submitted = Counter("runs_submitted_total", "Runs accepted for execution")
runs_rejected = Counter("ingestion_rejected_total", "Requests rejected+quarantined at the boundary", ["reason"])
runs_finished = Counter("runs_finished_total", "Finished runs", ["route", "status"])
run_latency = Histogram("run_e2e_seconds", "Submission to final result", buckets=LAT_BUCKETS)
cache_events = Counter("run_cache_total", "Whole-run cache", ["result"])
rate_limited = Counter("rate_limited_total", "Requests refused by rate limiter", ["client"])
guardrail_events = Counter("guardrail_events_total", "Guardrail triggers", ["stage", "flag"])

agent_tasks = Counter("agent_tasks_total", "Agent task outcomes", ["agent", "outcome"])
agent_steps = Histogram("agent_steps_per_run", "Reasoning steps per agent task", ["agent"], buckets=(1, 2, 3, 4, 5, 6, 8, 10, 15, 20))
agent_tool_calls = Histogram("agent_tool_calls_per_run", "Tool calls per agent task", ["agent"], buckets=(0, 1, 2, 3, 4, 5, 6, 8, 10, 15))
agent_task_latency = Histogram("agent_task_seconds", "Agent task latency", ["agent"], buckets=LAT_BUCKETS)
step_latency = Histogram("agent_step_seconds", "Per-step latency", ["agent", "step"], buckets=LAT_BUCKETS)
tool_calls = Counter("tool_calls_total", "Tool calls", ["tool", "outcome"])
verification = Counter("verification_total", "Verification outcomes", ["outcome"])
queue_depth = Gauge("queue_depth", "Pending messages", ["stream"])
redeliveries = Counter("queue_redeliveries_total", "Messages reclaimed from a dead/slow consumer", ["stream"])

llm_calls = Counter("llm_calls_total", "LLM calls", ["agent", "step", "outcome"])
llm_retries = Counter("llm_retries_total", "LLM retries", ["agent", "step"])
llm_latency = Histogram("llm_call_seconds", "LLM call latency", ["agent", "step"], buckets=LAT_BUCKETS)
llm_tokens = Counter("llm_tokens_total", "Tokens", ["agent", "step", "kind"])
llm_cost = Counter("llm_cost_usd_total", "Cost USD at reference rates", ["agent", "step"])
breaker_state = Gauge("circuit_breaker_open", "1=open 0.5=half-open 0=closed", ["name"])
degraded_runs = Counter("degraded_runs_total", "Runs completed on the degradation path", ["reason"])

index_info = Info("index", "Index freshness/version")
index_chunks = Gauge("index_chunks", "Chunks in index")
index_built_ts = Gauge("index_built_timestamp_seconds", "When the index was built (unix)")
index_throughput = Gauge("index_pipeline_chunks_per_second", "Pipeline throughput at last build")

feedback_total = Counter("feedback_total", "Caller feedback", ["satisfactory"])
live_quality = Gauge("live_quality_score", "Latest rolling quality score of sampled live runs", ["metric"])
live_quality_hist = Histogram("live_quality_sample", "Per-run quality score of sampled live runs", ["metric"],
                              buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0))
