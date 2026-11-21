"""Pre-login demo panel: a per-demo-line summary computed from each line's REAL data
(latest bill vs previous, its top driver, current-cycle data use). Only the configured demo lines,
only when the `demo-accounts-panel` flag is on (safe default: off)."""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from telco_common.billing import data_usage_pct
from telco_common.catalog import get_plan
from telco_common.demo import DEMO_SUBSCRIBERS
from telco_common.explainer import explain, fmt_money
from telco_common.flags import flag_bool
from telco_common.models import cycle_of

from ..db import get_engine
from ..deps import get_usage_state
from ..services.bills import list_bills, row_to_bill

router = APIRouter(prefix="/v1/demo", tags=["demo"])


def _in_sentence(label: str) -> str:
    """'Roaming data, zone Z2' -> 'roaming data in zone Z2' (only the first letter is lowered)."""
    return (label[:1].lower() + label[1:]).replace(", zone ", " in zone ")


def _summary(s) -> dict:
    plan = get_plan(s.plan_code)
    rows = list_bills(get_engine(), s.subscriber_id, 2)
    cycle = cycle_of(datetime.now(UTC))
    pct = data_usage_pct(plan, get_usage_state().counters(s.subscriber_id, cycle))
    headline = "No bills yet"
    if rows:
        cur = row_to_bill(rows[-1])
        prev = row_to_bill(rows[-2]) if len(rows) > 1 else None
        ex = explain(cur, prev)
        month = datetime(int(cur.cycle[:4]), int(cur.cycle[5:]), 1).strftime("%B")
        if ex.direction == "UP" and ex.drivers:
            headline = f"{month} bill +{fmt_money(ex.delta_total or 0, cur.currency)}, mainly {_in_sentence(ex.drivers[0].label)}"
        elif ex.direction == "DOWN" and ex.drivers:
            headline = f"{month} bill −{fmt_money(abs(ex.delta_total or 0), cur.currency)}, mainly {_in_sentence(ex.drivers[0].label)}"
        else:
            extra = sum(x.amount for x in cur.lines if x.category != "PLAN")
            headline = (f"{month} bill {fmt_money(cur.total, cur.currency)}"
                        + (f", {fmt_money(extra, cur.currency)} beyond the plan" if extra else ", plan fee only"))
    return {"subscriber_id": s.subscriber_id, "name": s.name, "country": s.country, "plan": plan.name,
            "headline": headline, "data_pct_this_cycle": round(pct, 0)}


@router.get("/accounts")
def accounts() -> list[dict]:
    if not flag_bool("demo-accounts-panel"):
        raise HTTPException(404, "not available")
    return [_summary(s) for s in DEMO_SUBSCRIBERS]
