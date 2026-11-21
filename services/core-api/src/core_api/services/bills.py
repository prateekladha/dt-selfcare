"""Bill run (cycle close) and bill queries.

A bill is immutable once issued (id = "<subscriber>:<cycle>", insert-if-absent), so re-running the
bill run is safe. Usage that arrives after the close (late records) stays in the cycle counters
and is picked up as an adjustment by the reconciliation job rather than mutating an issued invoice.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from telco_common.billing import Bill, rate
from telco_common.catalog import get_plan
from telco_common.models import cycle_of
from telco_common.usage_state import UsageState

from ..db import bills, payments, subscribers


def cycle_bounds(cycle: str) -> tuple[datetime, datetime]:
    y, m = map(int, cycle.split("-"))
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
    return start, end


def previous_cycle(cycle: str, n: int = 1) -> str:
    y, m = map(int, cycle.split("-"))
    m -= n
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def row_to_bill(row) -> Bill:
    return Bill.model_validate_json(row.payload)


def close_cycle(engine: Engine, state: UsageState, cycle: str, settle_before: str | None = None,
                due_days: int = 14) -> dict[str, int]:
    start, end = cycle_bounds(cycle)
    if end > datetime.now(UTC):
        raise ValueError(f"cycle {cycle} is not over yet")
    issued_at = end
    created = skipped = 0
    with engine.connect() as cx:
        subs = cx.execute(select(subscribers.c.id, subscribers.c.plan_code)).all()
    for sub_id, plan_code in subs:
        bill = rate(sub_id, cycle, get_plan(plan_code), state.counters(sub_id, cycle))
        with engine.begin() as cx:
            advance = cx.execute(select(func.coalesce(func.sum(payments.c.amount), 0)).where(
                payments.c.subscriber_id == sub_id, payments.c.purpose == "ADVANCE", payments.c.status == "PAID",
                payments.c.created_at >= start, payments.c.created_at < end)).scalar_one()
            paid = min(int(advance), bill.total)
            settled = settle_before is not None and cycle < settle_before
            if settled:
                paid = bill.total  # historical import: earlier invoices settled by direct debit
            try:
                cx.execute(insert(bills).values(
                    id=f"{sub_id}:{cycle}", subscriber_id=sub_id, cycle=cycle, plan_code=bill.plan_code,
                    currency=bill.currency, subtotal=bill.subtotal, vat=bill.vat, total=bill.total,
                    amount_paid=paid, status="PAID" if paid >= bill.total else "ISSUED",
                    issued_at=issued_at, due_date=issued_at + timedelta(days=due_days),
                    payload=bill.model_dump_json()))
                created += 1
            except IntegrityError:
                skipped += 1
    return {"created": created, "skipped_existing": skipped}


def list_bills(engine: Engine, subscriber_id: str, months: int = 12) -> list:
    with engine.connect() as cx:
        rows = cx.execute(select(bills).where(bills.c.subscriber_id == subscriber_id)
                          .order_by(bills.c.cycle.desc()).limit(months)).all()
    return list(reversed(rows))


def get_bill_row(engine: Engine, subscriber_id: str, cycle: str):
    with engine.connect() as cx:
        return cx.execute(select(bills).where(bills.c.subscriber_id == subscriber_id,
                                              bills.c.cycle == cycle)).first()


def unbilled(state: UsageState, subscriber_id: str, plan_code: str, now: datetime | None = None) -> Bill:
    cycle = cycle_of(now or datetime.now(UTC))
    return rate(subscriber_id, cycle, get_plan(plan_code), state.counters(subscriber_id, cycle))


def cli() -> None:
    from ..db import create_schema, get_engine
    from ..deps import get_usage_state

    ap = argparse.ArgumentParser(description="Close billing cycles and issue bills")
    ap.add_argument("--cycle", help="YYYY-MM; default: previous month")
    ap.add_argument("--backfill-months", type=int, default=0, help="also close N cycles before --cycle")
    ap.add_argument("--settle-before", help="mark bills of cycles before YYYY-MM as paid (history import)")
    a = ap.parse_args()
    engine = get_engine()
    create_schema(engine)
    target = a.cycle or previous_cycle(cycle_of(datetime.now(UTC)))
    for n in range(a.backfill_months, -1, -1):
        c = previous_cycle(target, n)
        print(json.dumps({"cycle": c, **close_cycle(engine, get_usage_state(), c, a.settle_before)}))
