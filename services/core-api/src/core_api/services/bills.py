"""Bill run (cycle close) and bill queries.

A bill is immutable once issued (id = "<subscriber>:<cycle>", insert-if-absent), so re-running the
bill run is safe. Usage that arrives after the close (late records) stays in the cycle counters
and is picked up as an adjustment by the reconciliation job rather than mutating an issued invoice.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from telco_common.billing import Bill, BillLine, LineCategory, rate, rate_segments
from telco_common.catalog import get_plan
from telco_common.models import cycle_of
from telco_common.usage_state import UsageState

from ..db import bills, payments, subscribers
from .plan_history import segments


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


def rate_cycle(state: UsageState, sub_id: str, cycle: str, plan_code: str, counters: dict[str, int],
               seg, adjustments: list[BillLine] | None = None) -> Bill:
    """One rating path for the bill run and the live unbilled amount.
    - no tariff change in the cycle: whole cycle on the tariff;
    - change + per-day usage known: each segment rated on its own tariff and its own days (exact);
    - change but no per-day detail (cycles recorded before per-day counters): blended allowances."""
    plan = get_plan(plan_code)
    if not seg:
        return rate(sub_id, cycle, plan, counters, adjustments)
    ranges, day = [], 1
    for _, days in seg:
        ranges.append((day, day + days - 1))
        day += days
    split = state.split_by_days(sub_id, cycle, ranges)
    if split is None:
        return rate(sub_id, cycle, plan, counters, adjustments, segments=seg)
    return rate_segments(sub_id, cycle, seg, split, adjustments)


def close_cycle(engine: Engine, state: UsageState, cycle: str, settle_before: str | None = None,
                due_days: int = 14) -> dict[str, int]:
    start, end = cycle_bounds(cycle)
    if end > datetime.now(UTC):
        raise ValueError(f"cycle {cycle} is not over yet")
    issued_at = end
    created = skipped = 0
    with engine.connect() as cx:
        subs = cx.execute(select(subscribers.c.id, subscribers.c.plan_code)).all()
    prev = previous_cycle(cycle)
    for sub_id, plan_code in subs:
        plan = get_plan(plan_code)
        counters = state.counters(sub_id, cycle)
        with engine.begin() as cx:
            if cx.execute(select(bills.c.id).where(bills.c.id == f"{sub_id}:{cycle}")).first():
                skipped += 1
                continue
            # Late usage: CDRs for the previous cycle that arrived after its bill was issued
            adjustments: list[BillLine] = []
            prev_row = cx.execute(select(bills).where(bills.c.id == f"{sub_id}:{prev}")).first()
            late_counters = None
            if prev_row is not None and prev_row.rated_counters:
                billed = {k: int(v) for k, v in json.loads(prev_row.rated_counters).items()}
                now_prev = state.counters(sub_id, prev)
                if now_prev != billed:
                    pseg = segments(engine, sub_id, prev, plan_code)
                    delta = (rate(sub_id, prev, plan, now_prev, segments=pseg).subtotal
                             - rate(sub_id, prev, plan, billed, segments=pseg).subtotal)
                    if delta:
                        y, m = map(int, prev.split("-"))
                        adjustments.append(BillLine(
                            code=f"ADJ:{prev}", category=LineCategory.ADJUSTMENT, quantity=1, unit="adjustment",
                            label=f"Late usage from {datetime(y, m, 1).strftime('%B %Y')} (CDRs received after that bill)",
                            amount=delta))
                    late_counters = now_prev
            bill = rate_cycle(state, sub_id, cycle, plan_code, counters, segments(engine, sub_id, cycle, plan_code),
                              adjustments)
            advance = cx.execute(select(func.coalesce(func.sum(payments.c.amount), 0)).where(
                payments.c.subscriber_id == sub_id, payments.c.purpose == "ADVANCE", payments.c.status == "PAID",
                payments.c.created_at >= start, payments.c.created_at < end)).scalar_one()
            paid = min(int(advance), bill.total)
            if settle_before is not None and cycle < settle_before:
                paid = bill.total  # historical import: earlier invoices settled by direct debit
            try:
                cx.execute(insert(bills).values(
                    id=f"{sub_id}:{cycle}", subscriber_id=sub_id, cycle=cycle, plan_code=bill.plan_code,
                    currency=bill.currency, subtotal=bill.subtotal, vat=bill.vat, total=bill.total,
                    amount_paid=paid, status="PAID" if paid >= bill.total else "ISSUED",
                    issued_at=issued_at, due_date=issued_at + timedelta(days=due_days),
                    payload=bill.model_dump_json(), rated_counters=json.dumps(counters)))
                if late_counters is not None:  # the adjustment now accounts for those late CDRs
                    cx.execute(update(bills).where(bills.c.id == prev_row.id).values(rated_counters=json.dumps(late_counters)))
                created += 1
                from .home_cache import bump_default

                bump_default(sub_id)
            except IntegrityError:
                skipped += 1
    return {"created": created, "skipped_existing": skipped}


def bill_summary(row) -> dict:
    return {"id": row.id, "cycle": row.cycle, "total": row.total, "subtotal": row.subtotal, "vat": row.vat,
            "currency": row.currency, "status": row.status, "amount_paid": row.amount_paid,
            "outstanding": max(0, row.total - row.amount_paid), "issued_at": row.issued_at.isoformat(),
            "due_date": row.due_date.isoformat()}


def list_bills(engine: Engine, subscriber_id: str, months: int = 12) -> list:
    with engine.connect() as cx:
        rows = cx.execute(select(bills).where(bills.c.subscriber_id == subscriber_id)
                          .order_by(bills.c.cycle.desc()).limit(months)).all()
    return list(reversed(rows))


def get_bill_row(engine: Engine, subscriber_id: str, cycle: str):
    with engine.connect() as cx:
        return cx.execute(select(bills).where(bills.c.subscriber_id == subscriber_id,
                                              bills.c.cycle == cycle)).first()


_LOOKUP = object()


def unbilled(state: UsageState, subscriber_id: str, plan_code: str, now: datetime | None = None,
             engine: Engine | None = None, seg=_LOOKUP) -> Bill:
    """Current cycle so far, pro-rated if the tariff changed this cycle. `seg` lets the home screen pass
    the segments from its event-invalidated cache instead of querying the tariff history."""
    from ..db import get_engine

    cycle = cycle_of(now or datetime.now(UTC))
    if seg is _LOOKUP:
        seg = segments(engine or get_engine(), subscriber_id, cycle, plan_code)
    return rate_cycle(state, subscriber_id, cycle, plan_code, state.counters(subscriber_id, cycle), seg)


def cli() -> None:
    from ..db import create_schema, get_engine
    from ..deps import get_usage_state

    ap = argparse.ArgumentParser(description="Close billing cycles and issue bills")
    ap.add_argument("--cycle", help="YYYY-MM; default: previous month")
    ap.add_argument("--backfill-months", type=int, default=0, help="also close N cycles before --cycle")
    ap.add_argument("--settle-before", help="mark bills of cycles before YYYY-MM as paid (history import)")
    ap.add_argument("--wait-quiet-seconds", type=int, default=0,
                    help="first wait until the aggregator has drained (offsets unchanged for N seconds)")
    a = ap.parse_args()
    if a.wait_quiet_seconds:
        import time

        st, last, quiet = get_usage_state(), None, 0
        while quiet < a.wait_quiet_seconds:
            snap = [st.committed_offset(p) for p in range(st.partitions)]
            quiet = quiet + 5 if snap == last else 0
            last = snap
            time.sleep(5)
        print(json.dumps({"aggregator_drained_at_offsets": last}))
    engine = get_engine()
    create_schema(engine)
    target = a.cycle or previous_cycle(cycle_of(datetime.now(UTC)))
    if a.settle_before == "LAST":  # history import: everything before the latest cycle was paid
        a.settle_before = target
    for n in range(a.backfill_months, -1, -1):
        c = previous_cycle(target, n)
        print(json.dumps({"cycle": c, **close_cycle(engine, get_usage_state(), c, a.settle_before)}))
