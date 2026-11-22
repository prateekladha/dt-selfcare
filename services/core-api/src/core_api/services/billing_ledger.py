"""Billing side of the payments boundary: apply PAYMENT_SUCCEEDED events to bills, exactly once.

payments-service never writes billing tables; it emits PAYMENT_SUCCEEDED (transactional outbox ->
payment-events stream). Billing consumes it here. The bill_payments row (PK = payment_id) and the bill
balance are written in one transaction, so replays and duplicate deliveries are no-ops.
"""
from __future__ import annotations

import json

from sqlalchemy import case, insert, select, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..db import bill_payments, bills


def apply_payment_event(engine: Engine, event: dict) -> bool:
    if event.get("type") != "PAYMENT_SUCCEEDED" or not event.get("bill_id"):
        return False
    try:
        with engine.begin() as cx:
            cx.execute(insert(bill_payments).values(payment_id=event["payment_id"], bill_id=event["bill_id"],
                                                    amount=int(event["amount"])))
            amount = int(event["amount"])
            # atomic increment (no read-modify-write): two payments settling at once can't lose a credit
            cx.execute(update(bills).where(bills.c.id == event["bill_id"]).values(
                amount_paid=bills.c.amount_paid + amount,
                status=case((bills.c.amount_paid + amount >= bills.c.total, "PAID"), else_="ISSUED")))
            bill = cx.execute(select(bills.c.subscriber_id).where(bills.c.id == event["bill_id"])).one()
        from .home_cache import bump_default

        bump_default(bill.subscriber_id)
        return True
    except IntegrityError:
        return False  # already applied


def drain_outbox_into_billing(engine: Engine) -> int:
    """Local/test helper mirroring outbox -> stream -> billing consumer in one step."""
    from ..db import outbox

    with engine.connect() as cx:
        rows = cx.execute(select(outbox).where(outbox.c.topic == "payment-events")).all()
    return sum(apply_payment_event(engine, json.loads(r.payload)) for r in rows)
