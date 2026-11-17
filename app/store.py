"""Redis-backed state: runs, per-run trace (full reconstruction, D5), quarantine (D1), feedback (L6),
idempotency, whole-run cache and per-client rate limiting (D7)."""
from __future__ import annotations
import hashlib, json, time
import redis

TRACE_TTL = 7 * 24 * 3600


class Store:
    def __init__(self, url: str):
        self.r = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=3,
                                      health_check_interval=30)

    def ping(self) -> bool:
        try:
            return bool(self.r.ping())
        except Exception:
            return False

    # ---- runs ----
    def create_run(self, run_id: str, petition: dict, client: str, extra: dict | None = None):
        now = time.time()
        rec = {"run_id": run_id, "status": "queued", "client": client, "created_at": now, "updated_at": now,
               "petition": json.dumps(petition), "research_rounds": 0}
        rec.update({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in (extra or {}).items()})
        self.r.hset(f"run:{run_id}", mapping=rec)
        self.r.expire(f"run:{run_id}", TRACE_TTL)

    def update_run(self, run_id: str, **fields):
        fields["updated_at"] = time.time()
        self.r.hset(f"run:{run_id}", mapping={k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in fields.items()})

    def get_run(self, run_id: str) -> dict | None:
        d = self.r.hgetall(f"run:{run_id}")
        if not d:
            return None
        for k in ("petition", "result", "dossier", "classification", "verify_feedback"):
            if k in d:
                d[k] = json.loads(d[k])
        for k in ("created_at", "updated_at", "finished_at"):
            if k in d:
                d[k] = float(d[k])
        return d

    def incr_run(self, run_id: str, field: str) -> int:
        return self.r.hincrby(f"run:{run_id}", field, 1)

    # ---- trace ----
    def trace(self, run_id: str, event: dict):
        event.setdefault("ts", time.time())
        self.r.rpush(f"trace:{run_id}", json.dumps(event, default=str))
        self.r.expire(f"trace:{run_id}", TRACE_TTL)

    def get_trace(self, run_id: str) -> list[dict]:
        return [json.loads(x) for x in self.r.lrange(f"trace:{run_id}", 0, -1)]

    # ---- quarantine ----
    def quarantine(self, payload, errors: list, client: str, reason: str) -> str:
        qid = "q_" + hashlib.sha256(f"{time.time()}{client}{payload}".encode()).hexdigest()[:12]
        self.r.lpush("quarantine", json.dumps({"quarantine_id": qid, "ts": time.time(), "client": client,
                                                "reason": reason, "errors": errors, "payload": payload}, default=str))
        self.r.ltrim("quarantine", 0, 9999)
        return qid

    def list_quarantine(self, n=50) -> list[dict]:
        return [json.loads(x) for x in self.r.lrange("quarantine", 0, n - 1)]

    # ---- feedback ----
    def add_feedback(self, fb: dict):
        fb["ts"] = time.time()
        self.r.rpush("feedback", json.dumps(fb))
        self.r.hincrby("feedback:agg", "satisfactory" if fb["satisfactory"] else "unsatisfactory", 1)

    def feedback_summary(self) -> dict:
        agg = {k: int(v) for k, v in self.r.hgetall("feedback:agg").items()}
        n = sum(agg.values())
        recent = [json.loads(x) for x in self.r.lrange("feedback", -20, -1)]
        reasons: dict = {}
        for f in (json.loads(x) for x in self.r.lrange("feedback", 0, -1)):
            if not f["satisfactory"]:
                reasons[f["reason"][:60]] = reasons.get(f["reason"][:60], 0) + 1
        return {"total": n, **agg, "satisfaction_rate": round(agg.get("satisfactory", 0) / n, 3) if n else None,
                "top_unsatisfactory_reasons": sorted(reasons.items(), key=lambda x: -x[1])[:5], "recent": recent}

    def add_quality_sample(self, sample: dict):
        self.r.rpush("quality:samples", json.dumps(sample))
        self.r.ltrim("quality:samples", -1000, -1)

    def quality_samples(self, n=200) -> list[dict]:
        return [json.loads(x) for x in self.r.lrange("quality:samples", -n, -1)]

    # ---- idempotency / cache ----
    def idem_get_or_set(self, client: str, key: str, run_id: str) -> str | None:
        k = f"idem:{client}:{key}"
        if self.r.set(k, run_id, nx=True, ex=24 * 3600):
            return None
        return self.r.get(k)

    def cache_get(self, key: str) -> dict | None:
        v = self.r.get(f"cache:run:{key}")
        return json.loads(v) if v else None

    def cache_set(self, key: str, result: dict, ttl: int):
        self.r.set(f"cache:run:{key}", json.dumps(result), ex=ttl)

    # ---- rate limiting: fixed 60 s window per client + hourly run budget ----
    def rate_check(self, client: str, per_min: int, per_hour: int) -> tuple[bool, int]:
        now = int(time.time())
        km, kh = f"rl:{client}:{now // 60}", f"rlh:{client}:{now // 3600}"
        p = self.r.pipeline()
        p.incr(km); p.expire(km, 61); p.incr(kh); p.expire(kh, 3601)
        cm, _, ch, _ = p.execute()
        if cm > per_min:
            return False, 60 - now % 60
        if ch > per_hour:
            return False, 3600 - now % 3600
        return True, 0


def cache_key(petition: dict, corpus_version: str, prompt_versions: dict, strategy: str) -> str:
    """Whole-run cache key: identical petition facts + same corpus + same prompts + same strategy => same answer.
    Identity fields (student_ref, client ref, submitted_on) are excluded: they do not change the eligibility position."""
    facts = {k: v for k, v in petition.items() if k not in ("student_ref", "client_petition_ref", "submitted_on")}
    blob = json.dumps({"p": facts, "c": corpus_version, "pv": prompt_versions, "s": strategy}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]
