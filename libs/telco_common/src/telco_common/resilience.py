"""Service-to-service resilience: explicit connect/read timeouts + a circuit breaker per dependency.

Every call to another service goes through `ServiceClient`, which:
- uses separate connect and read timeouts (a dead host fails in ~1 s, a slow one at the read budget);
- counts consecutive failures (timeouts, connection errors, 5xx) and OPENS the circuit after
  `fail_threshold`, failing fast for `reset_after_s` instead of piling requests onto a struggling service;
- after the cool-down lets ONE trial request through (HALF_OPEN): success closes the circuit, failure re-opens.
4xx responses are the caller's problem, not the dependency's, and don't trip the breaker; 429 doesn't
either (the dependency is protecting itself - back off via Retry-After instead).
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx


class CircuitOpenError(RuntimeError):
    def __init__(self, name: str, retry_in_s: float):
        super().__init__(f"circuit '{name}' is open; retry in {retry_in_s:.0f}s")
        self.name, self.retry_in_s = name, retry_in_s


@dataclass
class CircuitBreaker:
    name: str
    fail_threshold: int = 5
    reset_after_s: float = 30.0
    state: str = "CLOSED"
    failures: int = 0
    opened_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def before(self) -> None:
        with self._lock:
            if self.state == "OPEN":
                wait = self.reset_after_s - (time.monotonic() - self.opened_at)
                if wait > 0:
                    raise CircuitOpenError(self.name, wait)
                self.state = "HALF_OPEN"  # one trial request

    def success(self) -> None:
        with self._lock:
            self.state, self.failures = "CLOSED", 0

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.state == "HALF_OPEN" or self.failures >= self.fail_threshold:
                self.state, self.opened_at = "OPEN", time.monotonic()


def timeouts(connect_s: float = 1.0, read_s: float = 5.0) -> httpx.Timeout:
    return httpx.Timeout(connect=connect_s, read=read_s, write=read_s, pool=connect_s)


class ServiceClient:
    """Async HTTP client for one downstream service with timeouts + circuit breaker."""

    def __init__(self, name: str, base_url: str, connect_s: float = 1.0, read_s: float = 5.0,
                 fail_threshold: int = 5, reset_after_s: float = 30.0, transport: httpx.AsyncBaseTransport | None = None,
                 headers: dict | None = None):
        self.breaker = CircuitBreaker(name, fail_threshold, reset_after_s)
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeouts(connect_s, read_s),
                                         transport=transport, headers=headers or {})

    async def request(self, method: str, path: str, **kw) -> httpx.Response:
        self.breaker.before()
        try:
            r = await self._client.request(method, path, **kw)
        except (httpx.TimeoutException, httpx.TransportError):
            self.breaker.failure()
            raise
        if r.status_code >= 500:
            self.breaker.failure()
        else:
            self.breaker.success()
        return r
