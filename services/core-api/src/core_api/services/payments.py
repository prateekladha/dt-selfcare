"""Payment domain: idempotent creation, verified confirmation, exactly-once settlement.

Settlement (`mark_paid`) is reachable from two paths - the browser callback and the gateway
webhook - and is idempotent on the payment row (CREATED -> PAID happens once, guarded by a
conditional UPDATE). Bill balance and the PAYMENT_SUCCEEDED outbox event are written in the
same DB transaction, so downstream systems never miss or double-count a payment.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from ..db import bills, outbox, payments, utcnow
from .gateway import PaymentGateway


class PaymentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _public(row, checkout: dict | None = None) -> dict:
    return {"payment_id": row.id, "status": row.status, "amount": row.amount, "currency": row.currency,
            "purpose": row.purpose, "bill_id": row.bill_id, "gateway": row.gateway,
            "order_id": row.gateway_order_id, "checkout": checkout}


def create_payment(engine: Engine, gw: PaymentGateway, subscriber_id: str, idem_key: str, purpose: str,
                   amount: int, currency: str, bill_id: str | None) -> dict:
    with engine.connect() as cx:
        existing = cx.execute(select(payments).where(payments.c.subscriber_id == subscriber_id,
                                                     payments.c.idempotency_key == idem_key)).first()
    if existing:  # double tap / client retry: same order back, no second charge
        return _public(existing, {"provider": existing.gateway, "order_id": existing.gateway_order_id,
                                  **({"key_id": gw.key_id} if hasattr(gw, "key_id") else {})})
    if amount <= 0:
        raise PaymentError(409, "nothing to pay")
    pid = str(uuid.uuid4())
    order = gw.create_order(amount, currency, receipt=pid, notes={"payment_id": pid, "purpose": purpose})
    with engine.begin() as cx:
        cx.execute(insert(payments).values(
            id=pid, subscriber_id=subscriber_id, purpose=purpose, bill_id=bill_id, amount=amount,
            currency=currency, gateway=gw.name, gateway_order_id=order.order_id, status="CREATED",
            idempotency_key=idem_key))
        row = cx.execute(select(payments).where(payments.c.id == pid)).one()
    return _public(row, order.checkout)


def mark_paid(engine: Engine, order_id: str, gateway_payment_id: str) -> tuple[bool, dict | None]:
    """-> (newly_settled, payment). Safe to call any number of times from any path."""
    with engine.begin() as cx:
        row = cx.execute(select(payments).where(payments.c.gateway_order_id == order_id)).first()
        if row is None:
            return False, None
        res = cx.execute(update(payments)
                         .where(payments.c.id == row.id, payments.c.status == "CREATED")
                         .values(status="PAID", gateway_payment_id=gateway_payment_id, updated_at=utcnow()))
        if res.rowcount == 0:
            return False, _public(row)  # already settled by the other path
        if row.bill_id:
            bill = cx.execute(select(bills).where(bills.c.id == row.bill_id)).one()
            paid = bill.amount_paid + row.amount
            cx.execute(update(bills).where(bills.c.id == row.bill_id).values(
                amount_paid=paid, status="PAID" if paid >= bill.total else "ISSUED"))
        cx.execute(insert(outbox).values(topic="payment-events", event_key=row.subscriber_id, payload=json.dumps({
            "type": "PAYMENT_SUCCEEDED", "payment_id": row.id, "subscriber_id": row.subscriber_id,
            "amount": row.amount, "currency": row.currency, "purpose": row.purpose, "bill_id": row.bill_id,
            "gateway": row.gateway, "gateway_payment_id": gateway_payment_id})))
        settled = cx.execute(select(payments).where(payments.c.id == row.id)).one()
    return True, _public(settled)


def get_payment(engine: Engine, subscriber_id: str, payment_id: str):
    with engine.connect() as cx:
        return cx.execute(select(payments).where(payments.c.id == payment_id,
                                                 payments.c.subscriber_id == subscriber_id)).first()
