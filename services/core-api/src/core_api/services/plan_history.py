"""Tariff segments within a billing cycle, for pro-rating (see telco_common.billing.prorated_plan).

The change day is billed on the new tariff: a switch on 21 Nov in a 30-day cycle gives
[(old, 20 days), (new, 10 days)]. Several switches in one cycle give several segments.
"""
from __future__ import annotations

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.engine import Engine
from telco_common.catalog import Plan, get_plan


def segments(engine: Engine, subscriber_id: str, cycle: str, current_plan: str) -> list[tuple[Plan, int]] | None:
    """None when the tariff didn't change during the cycle (the common case: one PLAN_FEE line)."""
    from ..db import plan_changes
    from .bills import cycle_bounds

    start, end = cycle_bounds(cycle)
    with engine.connect() as cx:
        rows = cx.execute(select(plan_changes).where(plan_changes.c.subscriber_id == subscriber_id,
                                                     plan_changes.c.effective_at >= start,
                                                     plan_changes.c.effective_at < end)
                          .order_by(plan_changes.c.effective_at)).all()
    if not rows:
        return None
    total = (end.date() - start.date()).days
    out: list[tuple[Plan, int]] = []
    plan, day = rows[0].from_plan, 0
    for r in rows:
        at = r.effective_at if r.effective_at.tzinfo else r.effective_at.replace(tzinfo=UTC)
        idx = (at.date() - start.date()).days
        if idx > day:
            out.append((get_plan(plan), idx - day))
        plan, day = r.to_plan, max(day, idx)
    out.append((get_plan(plan), total - day))
    return out if len(out) > 1 else None
