"""Runtime configuration. Everything environment-specific comes from env vars (D4).
There are deliberately NO defaults for endpoints, credentials or model identifiers."""
from __future__ import annotations
import os


class ConfigError(RuntimeError):
    pass


def _req(name: str) -> str:
    v = os.environ.get(name)
    if v is None or v == "":
        raise ConfigError(f"Missing required environment variable {name}. See .env.example")
    return v


def _opt(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _bool(name: str, default: bool) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes", "on")


class Settings:
    """Lazy accessor so importing the module never fails; required values fail at first use."""

    # ---- infrastructure (required, injected) ----
    @property
    def redis_url(self) -> str: return _req("REDIS_URL")
    @property
    def llm_base_url(self) -> str: return _req("LLM_BASE_URL")
    @property
    def index_dir(self) -> str: return _req("INDEX_DIR")
    @property
    def corpus_dir(self) -> str: return _req("CORPUS_DIR")

    # ---- optional infra ----
    @property
    def llm_api_key(self) -> str: return _opt("LLM_API_KEY")
    @property
    def llm_model(self) -> str: return _opt("LLM_MODEL")
    @property
    def otlp_endpoint(self) -> str: return _opt("OTEL_EXPORTER_OTLP_ENDPOINT")
    @property
    def metrics_port(self) -> int: return _int("METRICS_PORT", 9100)
    @property
    def log_level(self) -> str: return _opt("LOG_LEVEL", "INFO")

    # ---- embeddings ----
    @property
    def embedding_backend(self) -> str: return _opt("EMBEDDING_BACKEND", "hashing")
    @property
    def embedding_model(self) -> str: return _opt("EMBEDDING_MODEL")

    # ---- agent limits (A1) ----
    @property
    def max_steps(self) -> int: return _int("AGENT_MAX_STEPS", 10)
    @property
    def max_tool_calls(self) -> int: return _int("AGENT_MAX_TOOL_CALLS", 10)
    @property
    def max_wall_s(self) -> float: return _float("AGENT_MAX_WALL_S", 25.0)
    @property
    def tool_timeout_s(self) -> float: return _float("TOOL_TIMEOUT_S", 5.0)
    @property
    def max_verify_retries(self) -> int: return _int("MAX_VERIFY_RETRIES", 1)
    @property
    def search_strategy(self) -> str: return _opt("SEARCH_STRATEGY", "agentic")  # agentic | single_pass
    @property
    def run_deadline_s(self) -> float: return _float("RUN_DEADLINE_S", 120.0)

    # ---- prompts (L2) ----
    @property
    def prompt_versions(self) -> dict:
        return {
            "classify": _opt("PROMPT_CLASSIFY", "v1"),
            "research_plan": _opt("PROMPT_RESEARCH_PLAN", "v1"),
            "draft_position": _opt("PROMPT_DRAFT_POSITION", "v2"),
        }

    # ---- resilience (A5) ----
    @property
    def llm_timeout_s(self) -> float: return _float("LLM_TIMEOUT_S", 10.0)
    @property
    def llm_max_retries(self) -> int: return _int("LLM_MAX_RETRIES", 2)
    @property
    def llm_backoff_base_s(self) -> float: return _float("LLM_BACKOFF_BASE_S", 0.3)
    @property
    def breaker_threshold(self) -> int: return _int("BREAKER_FAILURE_THRESHOLD", 4)
    @property
    def breaker_open_s(self) -> float: return _float("BREAKER_OPEN_S", 15.0)

    # ---- queue / worker ----
    @property
    def worker_role(self) -> str: return _opt("WORKER_ROLE", "researcher")
    @property
    def worker_threads(self) -> int: return _int("WORKER_THREADS", 4)
    @property
    def claim_idle_s(self) -> float: return _float("QUEUE_CLAIM_IDLE_S", 45.0)
    @property
    def max_deliveries(self) -> int: return _int("QUEUE_MAX_DELIVERIES", 3)

    # ---- cost (L5) reference rates in USD per 1k tokens ----
    @property
    def price_prompt_per_1k(self) -> float: return _float("PRICE_PROMPT_PER_1K", 0.00015)
    @property
    def price_completion_per_1k(self) -> float: return _float("PRICE_COMPLETION_PER_1K", 0.0006)

    # ---- caching / rate limit (D7) ----
    @property
    def run_cache_ttl_s(self) -> int: return _int("RUN_CACHE_TTL_S", 3600)
    @property
    def run_cache_enabled(self) -> bool: return _bool("RUN_CACHE_ENABLED", True)
    @property
    def rate_limit_per_min(self) -> int: return _int("RATE_LIMIT_PER_MIN", 60)
    @property
    def rate_limit_burst_runs_per_hour(self) -> int: return _int("RATE_LIMIT_RUNS_PER_HOUR", 600)

    # ---- quality sampling (L6) ----
    @property
    def live_score_sample_rate(self) -> float: return _float("LIVE_SCORE_SAMPLE_RATE", 0.25)

    # ---- gateway ----
    @property
    def gateway_provider(self) -> str: return _opt("GATEWAY_PROVIDER", "heuristic")  # heuristic | openai_compat
    @property
    def gateway_upstream_url(self) -> str: return _opt("GATEWAY_UPSTREAM_URL")
    @property
    def gateway_latency_ms(self) -> int: return _int("GATEWAY_LATENCY_MS", 400)
    @property
    def gateway_latency_jitter_ms(self) -> int: return _int("GATEWAY_LATENCY_JITTER_MS", 150)


settings = Settings()
