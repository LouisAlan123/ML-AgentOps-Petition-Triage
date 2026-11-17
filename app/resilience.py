"""A5: bounded retry with exponential backoff + jitter, and a thread-safe circuit breaker on the provider."""
from __future__ import annotations
import random, threading, time
from typing import Callable


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, name: str, failure_threshold: int = 4, open_seconds: float = 15.0, on_change: Callable | None = None):
        self.name, self.threshold, self.open_seconds = name, failure_threshold, open_seconds
        self.state, self.failures, self.opened_at = self.CLOSED, 0, 0.0
        self._lock = threading.Lock()
        self._trial_in_flight = False
        self.on_change = on_change or (lambda s: None)

    def _set(self, s):
        if s != self.state:
            self.state = s
            self.on_change(s)

    def before_call(self):
        with self._lock:
            if self.state == self.OPEN:
                if time.time() - self.opened_at >= self.open_seconds:
                    self._set(self.HALF_OPEN)
                else:
                    raise CircuitOpen(f"circuit {self.name} open")
            if self.state == self.HALF_OPEN:
                if self._trial_in_flight:
                    raise CircuitOpen(f"circuit {self.name} half-open, trial in flight")
                self._trial_in_flight = True

    def success(self):
        with self._lock:
            self.failures, self._trial_in_flight = 0, False
            self._set(self.CLOSED)

    def failure(self):
        with self._lock:
            self._trial_in_flight = False
            self.failures += 1
            if self.state == self.HALF_OPEN or self.failures >= self.threshold:
                self.opened_at = time.time()
                self._set(self.OPEN)

    def snapshot(self) -> dict:
        return {"name": self.name, "state": self.state, "failures": self.failures,
                "opened_for_s": round(time.time() - self.opened_at, 1) if self.state == self.OPEN else 0}


class RetryableError(Exception):
    def __init__(self, msg, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


def retry_call(fn: Callable, attempts: int, base: float, cap: float = 4.0, on_retry: Callable | None = None, deadline: float | None = None):
    """Call fn(); retry RetryableError up to `attempts` extra times with full-jitter exponential backoff."""
    last = None
    for i in range(attempts + 1):
        try:
            return fn(), i
        except RetryableError as e:
            last = e
            if i == attempts:
                break
            delay = e.retry_after if e.retry_after else random.uniform(0, min(cap, base * (2 ** i)))
            if deadline and time.time() + delay > deadline:
                break
            if on_retry:
                on_retry(i + 1, delay, e)
            time.sleep(delay)
    raise last
