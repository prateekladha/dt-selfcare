"""Shared runtime handles (DB engine, Valkey, usage state) with lazy init for tests."""
from __future__ import annotations

from functools import lru_cache

import valkey
from telco_common.usage_state import UsageState

from .settings import get_settings


@lru_cache
def get_valkey() -> valkey.Valkey:
    return valkey.Valkey.from_url(get_settings().valkey_url, socket_timeout=2, health_check_interval=30)


def get_usage_state() -> UsageState:
    return UsageState(get_valkey(), get_settings().stream_partitions)


class RateLimiter:
    """Fixed-window counter in Valkey: cost control for AI, brute-force control for login."""

    def __init__(self, r: valkey.Valkey):
        self.r = r

    def allow(self, key: str, limit: int, window_s: int) -> bool:
        k = f"rl:{key}"
        n = self.r.incr(k)
        if n == 1:
            self.r.expire(k, window_s)
        return n <= limit
