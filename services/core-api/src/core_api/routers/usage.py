"""Day-level usage for a billing month (trend chart on Overview and on each bill).

Served from the per-day fields the aggregator writes in the same atomic, exactly-once apply as the
cycle totals (`<counter>@DD`, keyed by the CDR's event day), so the days always add up to the totals and
late CDRs land on the day they happened. Valkey keeps a subscriber's monthly hash for 13 months.
"""
from __future__ import annotations

import calendar
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from telco_common.catalog import MB, get_plan
from telco_common.models import cycle_of

from ..db import get_engine, subscribers
from ..deps import get_usage_state
from ..security import Principal, current_principal

router = APIRouter(prefix="/v1/me/usage", tags=["usage"])


def _bucket(fields: dict[str, int]) -> dict[str, int]:
    data = sum(v for k, v in fields.items() if k == "DATA" or k.startswith("ROAMING_DATA:"))
    roam = sum(v for k, v in fields.items() if k.startswith("ROAMING_DATA:") and k != "ROAMING_DATA:EU")
    voice = sum(v for k, v in fields.items() if k in ("VOICE", "INTL_VOICE") or k.startswith("ROAMING_VOICE:"))
    sms = sum(v for k, v in fields.items() if k in ("SMS", "SMS_INTL", "MMS"))
    return {"data_mb": -(-data // MB), "roaming_mb": -(-roam // MB), "voice_min": -(-voice // 60), "sms": sms}


@router.get("/daily")
def daily(response: Response, cycle: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
          p: Principal = Depends(current_principal)) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    now = datetime.now(UTC)
    cycle = cycle or cycle_of(now)
    if cycle > cycle_of(now):
        raise HTTPException(422, "that month hasn't started")
    with get_engine().connect() as cx:
        plan_code = cx.execute(select(subscribers.c.plan_code).where(subscribers.c.id == p.subscriber_id)).scalar_one()
    y, m = map(int, cycle.split("-"))
    n_days = calendar.monthrange(y, m)[1]
    is_current = cycle == cycle_of(now)
    last_day = now.day if is_current else n_days
    per_day = get_usage_state().daily(p.subscriber_id, cycle)
    days = [{"day": d, **_bucket(per_day.get(d, {}))} for d in range(1, last_day + 1)]
    used = sum(d["data_mb"] - d["roaming_mb"] for d in days)
    plan = get_plan(plan_code)
    avg = used / max(1, last_day)
    return {"cycle": cycle, "days_in_month": n_days, "through_day": last_day, "current": is_current,
            "days": days, "data_allowance_mb": plan.data_mb,
            "totals": {k: sum(d[k] for d in days) for k in ("data_mb", "roaming_mb", "voice_min", "sms")},
            "avg_data_mb_per_day": round(avg), "projected_data_mb": round(avg * n_days) if is_current else None,
            "peak_day": max(days, key=lambda d: d["data_mb"])["day"] if days else None}
