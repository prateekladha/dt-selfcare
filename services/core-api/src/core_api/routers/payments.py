from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from telco_common.flags import flag_bool, flag_obj, flag_str

from ..db import bills, get_engine, subscribers
from ..deps import get_usage_state
from ..security import Principal, current_principal
from ..services.bills import unbilled
from ..services.gateway import PaymentGateway, Razorpay
from ..services.payments import PaymentError, create_payment, get_payment, reconcile_order
from ..services.resilience import CircuitOpen
from ..settings import get_settings

router = APIRouter(tags=["payments"])
_gateways: dict[str, PaymentGateway] = {}


def gateway_for(country: str, subscriber_id: str) -> PaymentGateway:
    name = flag_str("payment-gateway", country, subscriber_id)
    if name not in _gateways:
        s = get_settings()
        if name != "razorpay":
            raise HTTPException(503, f"payment gateway {name!r} not available")
        try:
            _gateways[name] = Razorpay(s.razorpay_key_id, s.razorpay_key_secret, s.razorpay_webhook_secret)
        except RuntimeError as e:  # keys not provisioned in Vault yet
            raise HTTPException(503, "payments are not configured") from e
    return _gateways[name]


class CreatePaymentIn(BaseModel):
    purpose: str = Field(pattern="^(BILL|ADVANCE)$")
    bill_id: str | None = None


class ConfirmIn(BaseModel):
    gateway_payment_id: str = Field(min_length=3, max_length=64)
    signature: str = Field(min_length=10, max_length=256)


@router.post("/v1/me/payments", status_code=201)
def create(body: CreatePaymentIn, p: Principal = Depends(current_principal),
           idempotency_key: str = Header(min_length=8, max_length=64)) -> dict:
    if not flag_bool("payments-enabled", p.country, p.subscriber_id):
        raise HTTPException(503, "payments are temporarily disabled")
    engine = get_engine()
    if body.purpose == "BILL":
        with engine.connect() as cx:
            bill = cx.execute(select(bills).where(bills.c.id == body.bill_id,
                                                  bills.c.subscriber_id == p.subscriber_id)).first()
        if bill is None:
            raise HTTPException(404, "bill not found")
        amount, currency, bill_id = bill.total - bill.amount_paid, bill.currency, bill.id
    else:
        with engine.connect() as cx:
            plan_code = cx.execute(select(subscribers.c.plan_code)
                                   .where(subscribers.c.id == p.subscriber_id)).scalar_one()
        current = unbilled(get_usage_state(), p.subscriber_id, plan_code, datetime.now(UTC))
        amount, currency, bill_id = current.total, current.currency, None
    try:
        return create_payment(engine, gateway_for(p.country, p.subscriber_id), p.subscriber_id, idempotency_key,
                              body.purpose, amount, currency, bill_id,
                              settle=flag_obj("payment-settlement", p.country, p.subscriber_id))
    except PaymentError as e:
        raise HTTPException(e.status, e.message) from e
    except CircuitOpen as e:
        raise HTTPException(503, "payment provider unavailable, try again shortly") from e
    except ValueError as e:
        raise HTTPException(502, str(e)) from e


@router.post("/v1/me/payments/{payment_id}/confirm")
def confirm(payment_id: str, body: ConfirmIn, p: Principal = Depends(current_principal)) -> dict:
    """Browser callback after checkout. Its signature is verified, but it is only a TRIGGER: the
    status is taken from the gateway's status API (reconcile_order)."""
    row = get_payment(get_engine(), p.subscriber_id, payment_id)
    if row is None:
        raise HTTPException(404, "payment not found")
    gw = gateway_for(p.country, p.subscriber_id)
    if not gw.verify_client_signature(row.gateway_order_id, body.gateway_payment_id, body.signature):
        raise HTTPException(400, "invalid payment signature")
    res = reconcile_order(get_engine(), gw, row.gateway_order_id, trigger="browser")
    return res.get("payment") or {"payment_id": row.id, "status": row.status}


@router.get("/v1/me/payments/{payment_id}")
def status(payment_id: str, refresh: bool = False, p: Principal = Depends(current_principal)) -> dict:
    row = get_payment(get_engine(), p.subscriber_id, payment_id)
    if row is None:
        raise HTTPException(404, "payment not found")
    if refresh and row.status != "PAID":
        res = reconcile_order(get_engine(), gateway_for(p.country, p.subscriber_id), row.gateway_order_id, trigger="poll")
        return res.get("payment") or {}
    return {"payment_id": row.id, "status": row.status, "amount": row.amount, "currency": row.currency,
            "gateway_status": row.gateway_status, "check_attempts": row.check_attempts,
            "next_check_at": row.next_check_at.isoformat() if row.next_check_at else None}


WEBHOOK_EVENTS = {"payment.authorized", "payment.captured", "payment.failed", "order.paid"}


@router.post("/v1/webhooks/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request, x_razorpay_signature: str = Header(default="")) -> dict:
    """Webhook = trigger only. After verifying the signature we take just the order id and re-read the
    real status from Razorpay's status API, so duplicated, delayed or out-of-order events (e.g. a
    'payment.failed' for a dropped attempt arriving after 'payment.captured') cannot corrupt state."""
    body = await request.body()
    s = get_settings()
    try:
        gw = _gateways.get("razorpay") or Razorpay(s.razorpay_key_id, s.razorpay_key_secret, s.razorpay_webhook_secret)
    except RuntimeError as e:
        raise HTTPException(503, "payments are not configured") from e
    if not gw.verify_webhook(body, x_razorpay_signature):
        raise HTTPException(401, "bad signature")
    event = await request.json()
    name = event.get("event")
    if name not in WEBHOOK_EVENTS:
        return {"ok": True, "ignored": name}
    payload = event.get("payload", {})
    order_id = (payload.get("payment", {}).get("entity", {}).get("order_id")
                or payload.get("order", {}).get("entity", {}).get("id"))
    if not order_id:
        return {"ok": True, "ignored": "no order id"}
    res = reconcile_order(get_engine(), gw, order_id, trigger="webhook")
    return {"ok": True, "event": name, "result": res["result"], "settled": res["result"] == "paid"}
