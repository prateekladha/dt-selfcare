"""Bill run (cycle close) and bill queries.

A bill is immutable once issued (id = "<subscriber>:<cycle>", insert-if-absent), so re-running the
bill run is safe. Usage that arrives after the close (late records) stays in the cycle counters
and is picked up as an adjustment by the reconciliation job rather than mutating an issued invoice.
"""
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from telco_common.billing import Bill, BillLine, LineCategory, rate, rate_segments
from telco_common.catalog import get_plan
from telco_common.models import cycle_of
from telco_common.usage_state import UsageState

from ..db import bill_payments, bills, payments, subscribers
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
               seg, adjustments: list[BillLine] | None = None, first_day: int = 1) -> Bill:
    """One rating path for the bill run and the live unbilled amount.
    - no tariff change in the cycle: whole cycle on the tariff;
    - change + per-day usage known: each segment rated on its own tariff and its own days (exact);
    - change but no per-day detail (cycles recorded before per-day counters): blended allowances.
    first_day > 1: the customer joined on that day, so fee and allowances cover only the days from then."""
    plan = get_plan(plan_code)
    start, end = cycle_bounds(cycle)
    month_days = (end.date() - start.date()).days
    if first_day > 1:   # joined mid-month: one segment (or the tariff segments) starting on the join day
        seg = _from_day(seg or [(plan, month_days)], first_day)
    elif not seg:
        return rate(sub_id, cycle, plan, counters, adjustments)
    ranges, day = [], first_day
    for _, days in seg:
        ranges.append((day, day + days - 1))
        day += days
    split = state.split_by_days(sub_id, cycle, ranges)
    if split is None:
        if first_day > 1:   # no per-day detail: whole usage on the active days
            return rate_segments(sub_id, cycle, seg[-1:] if len(seg) == 1 else seg, [counters] + [{}] * (len(seg) - 1),
                                 adjustments, month_days=month_days, first_day=first_day)
        return rate(sub_id, cycle, plan, counters, adjustments, segments=seg)
    return rate_segments(sub_id, cycle, seg, split, adjustments, month_days=month_days, first_day=first_day)


def _from_day(seg: list, first_day: int) -> list:
    """Drop the days before `first_day` from the front of the tariff segments."""
    drop, out = first_day - 1, []
    for p, days in seg:
        if drop >= days:
            drop -= days
            continue
        out.append((p, days - drop))
        drop = 0
    return out


def first_billable_day(engine: Engine, sub_id: str, cycle: str) -> int:
    """1 unless the customer joined during this cycle and has no earlier bill (then the join day), so a new
    customer doesn't pay a full month for a few days. Existing customers (any earlier bill) are never cut."""
    start, end = cycle_bounds(cycle)
    with engine.connect() as cx:
        created = cx.execute(select(subscribers.c.created_at).where(subscribers.c.id == sub_id)).scalar()
        if created is None:
            return 1
        created = created if created.tzinfo else created.replace(tzinfo=UTC)
        if not (start < created < end):
            return 1
        if cx.execute(select(bills.c.id).where(bills.c.subscriber_id == sub_id, bills.c.cycle < cycle)).first():
            return 1
    return created.day


def close_cycle(engine: Engine, state: UsageState, cycle: str, settle_before: str | None = None,
                due_days: int = 14) -> dict[str, int]:
    _start, end = cycle_bounds(cycle)
    if end > datetime.now(UTC):
        raise ValueError(f"cycle {cycle} is not over yet")
    issued_at = end
    created = skipped = 0
    with engine.connect() as cx:
        subs = cx.execute(select(subscribers.c.id, subscribers.c.plan_code)).all()
    prev = previous_cycle(cycle)
    failed: list[str] = []
    for sub_id, plan_code in subs:
        try:
            res = _close_one(engine, state, cycle, sub_id, plan_code, prev, end, issued_at, settle_before, due_days)
        except Exception as e:  # noqa: BLE001 - one customer's bad data must not stop everyone else's bill
            import structlog
            structlog.get_logger("bill-run").error("bill_failed", subscriber=sub_id[-4:], error=str(e)[:200])
            failed.append(sub_id)
            continue
        created += res == "created"
        skipped += res == "skipped"
    return {"created": created, "skipped_existing": skipped, "failed": len(failed)}


LATE_LOOKBACK = 3   # billed months checked for late usage on every bill run


def plan_at(engine: Engine, sub_id: str, when, current: str) -> str:
    """The tariff in force at `when` (from the tariff history): bills and late-usage adjustments are priced on
    the tariff of THAT month, not today's (a switch on the 1st before the bill run re-priced the closed month)."""
    from ..db import plan_changes

    with engine.connect() as cx:
        before = cx.execute(select(plan_changes.c.to_plan).where(
            plan_changes.c.subscriber_id == sub_id, plan_changes.c.effective_at < when)
            .order_by(plan_changes.c.effective_at.desc())).first()
        if before:
            return before.to_plan
        after = cx.execute(select(plan_changes.c.from_plan).where(
            plan_changes.c.subscriber_id == sub_id, plan_changes.c.effective_at >= when)
            .order_by(plan_changes.c.effective_at)).first()
    return after.from_plan if after else current


def _close_one(engine: Engine, state: UsageState, cycle: str, sub_id: str, current_plan: str, prev: str, end,
               issued_at, settle_before: str | None, due_days: int) -> str:
    bill_id = f"{sub_id}:{cycle}"
    plan_code = plan_at(engine, sub_id, end, current_plan)
    counters = state.counters(sub_id, cycle)
    with engine.begin() as cx:
        if cx.execute(select(bills.c.id).where(bills.c.id == bill_id)).first():
            return "skipped"
        # Late usage: records for an already-billed month that arrived after its bill was issued become an
        # adjustment on this bill. The last LATE_LOOKBACK billed months are checked (only the previous one used
        # to be, so a record arriving two months late was never billed).
        adjustments: list[BillLine] = []
        late_updates: list[tuple[str, dict]] = []
        for back in range(1, LATE_LOOKBACK + 1):
            old = previous_cycle(cycle, back)
            old_row = cx.execute(select(bills).where(bills.c.id == f"{sub_id}:{old}")).first()
            if old_row is None or not old_row.rated_counters:
                continue
            billed = {k: int(v) for k, v in json.loads(old_row.rated_counters).items()}
            now_old = state.counters(sub_id, old)
            if not now_old or now_old == billed:   # unchanged (or usage already expired from the live store)
                continue
            old_plan_code = plan_at(engine, sub_id, cycle_bounds(old)[1], current_plan)
            old_plan = get_plan(old_plan_code)
            oseg = segments(engine, sub_id, old, old_plan_code)
            delta = (rate(sub_id, old, old_plan, now_old, segments=oseg).subtotal
                     - rate(sub_id, old, old_plan, billed, segments=oseg).subtotal)
            if delta:
                y, m = map(int, old.split("-"))
                adjustments.append(BillLine(
                    code=f"ADJ:{old}", category=LineCategory.ADJUSTMENT, quantity=1, unit="adjustment",
                    label=f"Late usage from {datetime(y, m, 1).strftime('%B %Y')} (records received after that bill)",
                    amount=delta))
            late_updates.append((old_row.id, now_old))
        bill = rate_cycle(state, sub_id, cycle, plan_code, counters, segments(engine, sub_id, cycle, plan_code),
                          adjustments, first_day=first_billable_day(engine, sub_id, cycle))
        # Advance payments: every PAID advance up to the end of this cycle that no bill has credited yet. Keyed
        # by payment (bill_payments), so one that settled late (PENDING at bill run) is credited by the next run
        # instead of being lost, and none is ever credited twice.
        adv = cx.execute(select(payments.c.id, payments.c.amount).where(
            payments.c.subscriber_id == sub_id, payments.c.purpose == "ADVANCE", payments.c.status == "PAID",
            payments.c.created_at < end, payments.c.id.not_in(select(bill_payments.c.payment_id)))).all()
        paid = min(sum(int(a.amount) for a in adv), bill.total)
        if settle_before is not None and cycle < settle_before:
            paid = bill.total  # historical import: earlier invoices settled by direct debit
        try:
            cx.execute(insert(bills).values(
                id=bill_id, subscriber_id=sub_id, cycle=cycle, plan_code=bill.plan_code,
                currency=bill.currency, subtotal=bill.subtotal, vat=bill.vat, total=bill.total,
                amount_paid=paid, status="PAID" if paid >= bill.total else "ISSUED",
                issued_at=issued_at, due_date=issued_at + timedelta(days=due_days),
                payload=bill.model_dump_json(), rated_counters=json.dumps(counters)))
            for a in adv:   # mark the advances as credited to this bill
                cx.execute(insert(bill_payments).values(payment_id=a.id, bill_id=bill_id, amount=int(a.amount)))
            for old_id, now_counters in late_updates:   # those late records are now accounted for
                cx.execute(update(bills).where(bills.c.id == old_id).values(rated_counters=json.dumps(now_counters)))
        except IntegrityError:
            return "skipped"
    from .home_cache import bump_default

    bump_default(sub_id)
    return "created"


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
    engine = engine or get_engine()
    if seg is _LOOKUP:
        seg = segments(engine, subscriber_id, cycle, plan_code)
    return rate_cycle(state, subscriber_id, cycle, plan_code, state.counters(subscriber_id, cycle), seg,
                      first_day=first_billable_day(engine, subscriber_id, cycle))


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
