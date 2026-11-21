"""Payment domain: idempotent creation, verified confirmation, exactly-once settlement.

Settlement (`mark_paid`) is reachable from two paths - the browser callback and the gateway
webhook - and is idempotent on the payment row (CREATED -> PAID happens once, guarded by a
conditional UPDATE). Bill balance and the PAYMENT_SUCCEEDED outbox event are written in the
same DB transaction, so downstream systems never miss or double-count a payment.
"""
from __future__ import annotations

import json
import uuid
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from ..db import outbox, payments, utcnow
from .gateway import PaymentGateway


class PaymentError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def settlement(amount: int, currency: str, policy: dict) -> tuple[int, str, str | None]:
    """Bill amount -> (amount, currency, fx) the gateway is asked to charge.

    policy comes from the per-country `payment-settlement` flag, e.g.
    {"currency": "INR", "fx": {"EUR": 90.0}}. Razorpay's UPI rail is INR-only; an EU gateway
    (Adyen/Stripe) would use {"currency": "native"} and charge the bill currency directly.
    """
    target = (policy or {}).get("currency", "native")
    if target in ("native", currency):
        return amount, currency, None
    rate = (policy.get("fx") or {}).get(currency)
    if not rate:
        raise PaymentError(503, f"no settlement rate configured for {currency}->{target}")
    return int((Decimal(amount) * Decimal(str(rate))).quantize(Decimal(1), rounding=ROUND_HALF_UP)), target, str(rate)


def _public(row, checkout: dict | None = None) -> dict:
    return {"payment_id": row.id, "status": row.status, "amount": row.amount, "currency": row.currency,
            "charged_amount": row.charged_amount or row.amount, "charged_currency": row.charged_currency or row.currency,
            "fx_rate": row.fx_rate,
            "purpose": row.purpose, "bill_id": row.bill_id, "gateway": row.gateway,
            "order_id": row.gateway_order_id, "checkout": checkout}


def create_payment(engine: Engine, gw: PaymentGateway, subscriber_id: str, idem_key: str, purpose: str,
                   amount: int, currency: str, bill_id: str | None, settle: dict | None = None) -> dict:
    with engine.connect() as cx:
        existing = cx.execute(select(payments).where(payments.c.subscriber_id == subscriber_id,
                                                     payments.c.idempotency_key == idem_key)).first()
    if existing:  # double tap / client retry: same order back, no second charge
        return _public(existing, {"provider": existing.gateway, "order_id": existing.gateway_order_id,
                                  **({"key_id": gw.key_id} if hasattr(gw, "key_id") else {})})
    if amount <= 0:
        raise PaymentError(409, "nothing to pay")
    pid = str(uuid.uuid4())
    charged, charged_cur, fx = settlement(amount, currency, settle or {})
    order = gw.create_order(charged, charged_cur, receipt=pid, notes={
        "payment_id": pid, "purpose": purpose, "bill_amount": f"{amount} {currency}", **({"fx": fx} if fx else {})})
    with engine.begin() as cx:
        cx.execute(insert(payments).values(
            id=pid, subscriber_id=subscriber_id, purpose=purpose, bill_id=bill_id, amount=amount,
            currency=currency, charged_amount=charged, charged_currency=charged_cur, fx_rate=fx,
            gateway=gw.name, gateway_order_id=order.order_id, status="CREATED",
            idempotency_key=idem_key))
        row = cx.execute(select(payments).where(payments.c.id == pid)).one()
    return _public(row, order.checkout)


def mark_paid(engine: Engine, order_id: str, gateway_payment_id: str) -> tuple[bool, dict | None]:
    """-> (newly_settled, payment). Safe to call any number of times from any path."""
    with engine.begin() as cx:
        row = cx.execute(select(payments).where(payments.c.gateway_order_id == order_id)).first()
        if row is None:
            return False, None
        res = cx.execute(update(payments)  # PAID is terminal: only a non-PAID row can transition (exactly once)
                         .where(payments.c.id == row.id, payments.c.status != "PAID")
                         .values(status="PAID", gateway_payment_id=gateway_payment_id, gateway_status="captured",
                                 next_check_at=None, updated_at=utcnow()))
        if res.rowcount == 0:
            return False, _public(row)  # already settled by the other path
        # Billing is a different service: it learns about the payment from PAYMENT_SUCCEEDED (outbox).
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


# ---------------- reconciliation: the gateway status API is the only source of truth ----------------
FINAL = {"PAID", "EXPIRED"}


def next_delay_s(attempt: int, strategy: str = "exponential", base: float = 30, factor: float = 2.0,
                 cap: float = 3600, poly: tuple[float, float, float] = (20.0, 10.0, 30.0)) -> float:
    """Backoff between status checks of a non-final payment.

    exponential: base * factor^n      (30 s, 60 s, 2 m, 4 m, 8 m ... capped at 1 h)
    polynomial:  a*n^2 + b*n + c       (30 s, 60 s, 130 s, 240 s ... capped) - gentler early growth
    """
    if strategy == "polynomial":
        a, b, c = poly
        d = a * attempt * attempt + b * attempt + c
    else:
        d = base * (factor ** attempt)
    return min(cap, d)


def reconcile_order(engine: Engine, gw, order_id: str, trigger: str, now=None, expire_after_s: int = 24 * 3600,
                    strategy: str = "exponential") -> dict:
    """Ask the gateway for the order's real status and apply it with guardrails.

    Called by every path - browser callback, webhook, reconciler - but none of their payloads are trusted;
    only GET /orders/{id} + /orders/{id}/payments decide. Event order therefore doesn't matter:
      - a late 'payment.failed' / 'user dropped' after a capture cannot undo PAID (terminal state)
      - a failed attempt followed by a successful retry on the same order still ends PAID
      - an authorised-but-not-captured payment is captured by us, then PAID
    """
    from datetime import UTC, datetime, timedelta

    now = now or datetime.now(UTC)
    with engine.connect() as cx:
        row = cx.execute(select(payments).where(payments.c.gateway_order_id == order_id)).first()
    if row is None:
        return {"result": "unknown_order"}
    if row.status == "PAID":
        return {"result": "already_paid", "payment": _public(row)}

    st = gw.order_status(order_id)
    if st.state == "AUTHORIZED" and gw.capture(st.payment_id, row.charged_amount or row.amount,
                                                row.charged_currency or row.currency):
        st = type(st)("CAPTURED", st.payment_id, st.order_status, st.attempts)
    if st.state == "CAPTURED":
        newly, pay = mark_paid(engine, order_id, st.payment_id)
        with engine.begin() as cx:
            cx.execute(update(payments).where(payments.c.id == row.id, payments.c.settled_by.is_(None))
                       .values(settled_by=trigger))
        return {"result": "paid" if newly else "already_paid", "payment": pay, "gateway": st.attempts}

    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    age = (now - created).total_seconds()
    attempt = (row.check_attempts or 0) + 1
    if age >= expire_after_s:
        new_status = "EXPIRED" if st.state in ("NO_ATTEMPT", "ATTEMPTED") else "FAILED"
        next_at = None if new_status == "EXPIRED" else now + timedelta(seconds=next_delay_s(attempt, strategy, cap=6 * 3600))
    else:
        new_status = "FAILED" if st.state == "FAILED" else ("PENDING" if st.state == "ATTEMPTED" else row.status)
        next_at = now + timedelta(seconds=next_delay_s(attempt, strategy))
    with engine.begin() as cx:
        cx.execute(update(payments).where(payments.c.id == row.id, payments.c.status != "PAID").values(
            status=new_status, gateway_status=st.order_status or st.state.lower(), check_attempts=attempt,
            last_checked_at=now, next_check_at=next_at, updated_at=utcnow()))
        fresh = cx.execute(select(payments).where(payments.c.id == row.id)).one()
    return {"result": new_status.lower(), "payment": _public(fresh), "gateway": st.attempts,
            "next_check_at": next_at.isoformat() if next_at else None}


def due_for_check(engine: Engine, now, limit: int = 50) -> list[str]:
    """Non-final payments whose next check is due (or never scheduled). After an outage everything
    overdue is due at once, so statuses catch up as soon as the system is back."""
    from sqlalchemy import or_

    with engine.connect() as cx:
        rows = cx.execute(select(payments.c.gateway_order_id).where(
            payments.c.status.not_in(list(FINAL)), payments.c.gateway_order_id.is_not(None),
            or_(payments.c.next_check_at.is_(None), payments.c.next_check_at <= now))
            .order_by(payments.c.created_at).limit(limit)).all()
    return [r[0] for r in rows]
