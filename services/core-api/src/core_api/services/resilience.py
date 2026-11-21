"""Minimal circuit breaker for outbound dependencies (LLM, payment gateway).

closed -> (N consecutive failures) -> open for `reset_s` -> half-open (one trial call) -> closed/open.
While open we fail fast and the caller uses its fallback, instead of piling up slow requests.
"""
from __future__ import annotations

import threading
import time

from prometheus_client import Gauge

BREAKER_OPEN = Gauge("circuit_breaker_open", "1 if the breaker is open", ["name"])


class CircuitOpen(Exception):
    pass


class CircuitBreaker:
    def __init__(self, name: str, failures: int = 5, reset_s: float = 30.0):
        self.name, self.max_failures, self.reset_s = name, failures, reset_s
        self._failures, self._opened_at, self._lock = 0, 0.0, threading.Lock()

    def before(self) -> None:
        with self._lock:
            if self._failures >= self.max_failures and time.monotonic() - self._opened_at < self.reset_s:
                raise CircuitOpen(self.name)

    def success(self) -> None:
        with self._lock:
            self._failures = 0
            BREAKER_OPEN.labels(self.name).set(0)

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.max_failures:
                self._opened_at = time.monotonic()
                BREAKER_OPEN.labels(self.name).set(1)
