"""Cache-aside read model for the home screen (Valkey), invalidated by events, not by guessing TTLs.

The slow, rarely-changing parts of the home screen (profile, 12 bills, notifications) are cached per
subscriber under a version key. Every writer that changes them bumps the version (bill run, payment
applied, notification created/read, order completed), so the next read rebuilds; the TTL only bounds
memory. Live usage and the unbilled amount are NOT cached: they come straight from the usage state
(already in Valkey, updated by the stream).
"""
from __future__ import annotations

import json
from collections.abc import Callable

TTL_S = 6 * 3600


def _ver_key(sub: str) -> str:
    return f"hv:{{{sub}}}"


def bump(r, sub: str) -> None:
    """Invalidate the cached home read model for one subscriber (best effort; TTL bounds staleness)."""
    try:
        r.incr(_ver_key(sub))
    except Exception:  # noqa: BLE001, S110 - a failed bump must never fail the write it follows
        pass


def bump_default(sub: str) -> None:
    from ..deps import get_valkey

    try:
        bump(get_valkey(), sub)
    except Exception:  # noqa: BLE001, S110
        pass


def get_or_build(r, sub: str, build: Callable[[], dict]) -> tuple[dict, bool]:
    try:
        ver = r.get(_ver_key(sub)) or b"0"
        key = f"home:{{{sub}}}:{ver.decode() if isinstance(ver, bytes) else ver}"
        hit = r.get(key)
        if hit:
            return json.loads(hit), True
    except Exception:  # noqa: BLE001 - cache down: serve from the database
        return build(), False
    snap = build()
    try:
        r.setex(key, TTL_S, json.dumps(snap, default=str))
    except Exception:  # noqa: BLE001, S110
        pass
    return snap, False
