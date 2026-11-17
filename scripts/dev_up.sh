#!/usr/bin/env bash
# Run the whole system WITHOUT Docker (for development / machines without Docker). Needs redis-server and
# `pip install -r requirements.txt`. Usage: scripts/dev_up.sh [researcher_count]   Stop: scripts/dev_down.sh
set -euo pipefail
cd "$(dirname "$0")/.."
N=${1:-1}
set -a; source .env.example; [ -f .env ] && source .env; set +a
export REDIS_URL=redis://localhost:6379/0 LLM_BASE_URL=http://localhost:8080 INDEX_DIR=${DEV_INDEX_DIR:-./.data/index} CORPUS_DIR=./corpus PYTHONPATH=. OTEL_EXPORTER_OTLP_ENDPOINT=${DEV_OTLP:-}
mkdir -p .data/logs "$INDEX_DIR"
redis-cli ping >/dev/null 2>&1 || (redis-server --daemonize yes --appendonly yes --dir .data >/dev/null)
python -m app.corpus.synth >/dev/null; python -m app.corpus.pipeline index >/dev/null
nohup python -m uvicorn app.gateway.main:app --port 8080 > .data/logs/gateway.log 2>&1 & echo $! > .data/gateway.pid
for i in $(seq 1 "$N"); do METRICS_PORT=$((9100+i)) WORKER_ROLE=researcher nohup python -m app.worker.main > .data/logs/researcher$i.log 2>&1 & echo $! > .data/researcher$i.pid; done
METRICS_PORT=9200 WORKER_ROLE=adjudicator nohup python -m app.worker.main > .data/logs/adjudicator.log 2>&1 & echo $! > .data/adjudicator.pid
nohup python -m uvicorn app.api.main:app --port 8000 > .data/logs/api.log 2>&1 & echo $! > .data/api.pid
for i in $(seq 1 30); do curl -sf localhost:8000/ready >/dev/null && break; sleep 0.5; done
echo "up: api http://localhost:8000  gateway :8080  researchers=$N"
