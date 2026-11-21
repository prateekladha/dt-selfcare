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

TTL_S = 24 * 3600  # correctness comes from event invalidation; TTL only bounds memory


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


def build_snapshot(engine, sub_id: str) -> dict:
    """The cached part of the home screen: profile, last 12 bills, last 10 notifications."""
    from sqlalchemy import select

    from ..db import notifications, subscribers
    from .bills import bill_summary, list_bills

    with engine.connect() as cx:
        sub = cx.execute(select(subscribers).where(subscribers.c.id == sub_id)).first()
        if sub is None:
            raise LookupError(sub_id)
        notes = cx.execute(select(notifications).where(notifications.c.subscriber_id == sub_id)
                           .order_by(notifications.c.created_at.desc()).limit(10)).all()
    return {"sub": {"id": sub.id, "name": sub.name, "email": sub.email, "country": sub.country, "plan_code": sub.plan_code},
            "bills": [bill_summary(b) for b in list_bills(engine, sub_id, 12)],
            "notes": [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body,
                       "created_at": n.created_at.isoformat(), "read": n.read_at is not None} for n in notes]}


def warm(r, engine, sub_id: str) -> None:
    """Bill run: pre-build the snapshot so bill-day traffic hits the cache, not the database."""
    get_or_build(r, sub_id, lambda: build_snapshot(engine, sub_id))
