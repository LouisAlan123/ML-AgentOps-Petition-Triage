"""Generates observability/grafana/dashboards/petition_triage.json (A6, D5, D2, L5, L6)."""
import json
P = []
def ts(title, exprs, unit="short", w=8, h=7):
    P.append({"type": "timeseries", "title": title, "datasource": {"type": "prometheus", "uid": "prom"},
              "fieldConfig": {"defaults": {"unit": unit}}, "gridPos": {"w": w, "h": h},
              "targets": [{"expr": e, "legendFormat": l, "refId": chr(65 + i)} for i, (e, l) in enumerate(exprs)]})
def stat(title, expr, unit="short", w=4):
    P.append({"type": "stat", "title": title, "datasource": {"type": "prometheus", "uid": "prom"}, "fieldConfig": {"defaults": {"unit": unit}},
              "gridPos": {"w": w, "h": 4}, "targets": [{"expr": expr, "refId": "A"}]})
R = "[2m]"
stat("p95 run latency (SLO 10 s)", f"histogram_quantile(0.95, sum(rate(run_e2e_seconds_bucket{R})) by (le))", "s")
stat("Throughput (runs/s)", f"sum(rate(runs_finished_total{R}))", "reqps")
stat("Error rate", f"(sum(rate(runs_finished_total{{status='failed'}}{R})) or vector(0)) / clamp_min(sum(rate(runs_finished_total{R})),1e-9)", "percentunit")
stat("Availability (targets up)", "avg(up{job=~'api|llm-gateway|researcher|adjudicator'})", "percentunit")
stat("Circuit breaker open", "max(circuit_breaker_open)")
stat("Index age", "time() - max(index_built_timestamp_seconds)", "s")
ts("End-to-end run latency p50 / p95", [(f"histogram_quantile(0.5, sum(rate(run_e2e_seconds_bucket{R})) by (le))", "p50"), (f"histogram_quantile(0.95, sum(rate(run_e2e_seconds_bucket{R})) by (le))", "p95")], "s")
ts("Throughput and error rate", [(f"sum(rate(runs_finished_total{R})) by (status)", "{{status}}"), (f"sum(rate(http_requests_total{{code=~'5..'}}{R}))", "http 5xx")], "reqps")
ts("Routes", [(f"sum(rate(runs_finished_total{R})) by (route)", "{{route}}")], "reqps")
ts("Task success rate per agent", [(f"sum(rate(agent_tasks_total{{outcome='success'}}{R})) by (agent) / sum(rate(agent_tasks_total{R})) by (agent)", "{{agent}}")], "percentunit")
ts("Steps and tool calls per run", [(f"sum(rate(agent_steps_per_run_sum{R})) by (agent) / sum(rate(agent_steps_per_run_count{R})) by (agent)", "steps {{agent}}"),
                                    (f"sum(rate(agent_tool_calls_per_run_sum{R})) by (agent) / sum(rate(agent_tool_calls_per_run_count{R})) by (agent)", "tool calls {{agent}}")])
ts("Latency per step p95", [(f"histogram_quantile(0.95, sum(rate(agent_step_seconds_bucket{R})) by (le, agent, step))", "{{agent}}.{{step}}")], "s")
ts("Agent task latency p95", [(f"histogram_quantile(0.95, sum(rate(agent_task_seconds_bucket{R})) by (le, agent))", "{{agent}}")], "s")
ts("Retry rate (LLM)", [(f"sum(rate(llm_retries_total{R})) by (agent) / clamp_min(sum(rate(llm_calls_total{R})) by (agent),1e-9)", "{{agent}}")], "percentunit")
ts("LLM calls by outcome", [(f"sum(rate(llm_calls_total{R})) by (outcome)", "{{outcome}}")], "reqps")
ts("Container uptime", [("time() - container_start_time_seconds{name=~'.+'}", "{{name}}"), ("time() - process_start_time_seconds", "{{job}} {{instance}}")], "s")
ts("CPU per container", [("sum(rate(container_cpu_usage_seconds_total{name=~'.+'}[1m])) by (name)", "{{name}}")], "percentunit")
ts("Memory per container", [("sum(container_memory_usage_bytes{name=~'.+'}) by (name)", "{{name}}")], "bytes")
ts("Queue depth", [("max(queue_depth) by (stream)", "{{stream}}")])
ts("Cost per step (USD/s at reference rates)", [(f"sum(rate(llm_cost_usd_total{R})) by (agent, step)", "{{agent}}.{{step}}")], "currencyUSD")
ts("Tokens per step", [(f"sum(rate(llm_tokens_total{R})) by (step, kind)", "{{step}} {{kind}}")])
ts("Live quality (sampled runs)", [("max(live_quality_score) by (metric)", "{{metric}}")], "percentunit")
ts("Feedback", [("sum(feedback_total) by (satisfactory)", "satisfactory={{satisfactory}}")])
ts("Cache, rate limiting, guardrails, quarantine", [(f"sum(rate(run_cache_total{R})) by (result)", "cache {{result}}"), (f"sum(rate(rate_limited_total{R}))", "rate limited"),
    (f"sum(rate(guardrail_events_total{R})) by (stage, flag)", "guard {{stage}} {{flag}}"), (f"sum(rate(ingestion_rejected_total{R})) by (reason)", "quarantined {{reason}}")])
ts("Verification and degradation", [(f"sum(rate(verification_total{R})) by (outcome)", "verify {{outcome}}"), (f"sum(rate(degraded_runs_total{R})) by (reason)", "degraded {{reason}}"),
                                    (f"sum(rate(queue_redeliveries_total{R}))", "redeliveries")])
ts("Index freshness / pipeline throughput", [("max(index_chunks)", "chunks"), ("max(index_pipeline_chunks_per_second)", "pipeline chunks/s")])
x = y = 0
for p in P:
    w, h = p["gridPos"]["w"], p["gridPos"]["h"]
    if x + w > 24: x, y = 0, y + h
    p["gridPos"].update(x=x, y=y); p["id"] = len([q for q in P if "id" in q]) + 1; x += w
json.dump({"title": "Petition Triage", "uid": "petition-triage", "schemaVersion": 39, "time": {"from": "now-30m", "to": "now"}, "refresh": "5s", "panels": P},
          open("observability/grafana/dashboards/petition_triage.json", "w"), indent=1)
print(len(P), "panels")
