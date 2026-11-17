"""Redis Streams work queue between submission and execution (A4). Consumer groups give at-least-once delivery:
a message read by a worker that dies is reclaimed by another worker after QUEUE_CLAIM_IDLE_S (A2 failure handling)."""
from __future__ import annotations
import redis

RESEARCH = "stream:research"
ADJUDICATE = "stream:adjudicate"
GROUPS = {RESEARCH: "researchers", ADJUDICATE: "adjudicators"}


def ensure_groups(r: redis.Redis):
    for s, g in GROUPS.items():
        try:
            r.xgroup_create(s, g, id="0", mkstream=True)
        except redis.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise


def publish(r: redis.Redis, stream: str, fields: dict) -> str:
    return r.xadd(stream, {k: str(v) for k, v in fields.items()}, maxlen=100000, approximate=True)


class Consumer:
    def __init__(self, r: redis.Redis, stream: str, name: str):
        self.r, self.stream, self.group, self.name = r, stream, GROUPS[stream], name

    def read(self, block_ms=1000) -> list[tuple[str, dict]]:
        res = self.r.xreadgroup(self.group, self.name, {self.stream: ">"}, count=1, block=block_ms)
        return [(mid, f) for _, msgs in (res or []) for mid, f in msgs]

    def reclaim(self, min_idle_ms: int) -> list[tuple[str, dict]]:
        res = self.r.xautoclaim(self.stream, self.group, self.name, min_idle_time=min_idle_ms, start_id="0-0", count=5)
        return [(mid, f) for mid, f in res[1] if f]

    def deliveries(self, mid: str) -> int:
        p = self.r.xpending_range(self.stream, self.group, min=mid, max=mid, count=1)
        return int(p[0]["times_delivered"]) if p else 1

    def ack(self, mid: str):
        self.r.xack(self.stream, self.group, mid)
        self.r.xdel(self.stream, mid)

    def depth(self) -> int:
        try:
            info = self.r.xinfo_groups(self.stream)
            g = next(x for x in info if x["name"] == self.group)
            return int(g.get("lag") or 0) + int(g.get("pending") or 0)
        except Exception:
            return -1
