"""Customer-facing API. Every route is scoped to the token's subscriber.

GET /v1/me/home is the BFF call: one consistent snapshot for the whole home screen, stamped with
`as_of` and per-widget `version`s, so the SSR page never renders widgets from different moments.
Responses carry `Cache-Control: private, no-store`: personalised data is never cached by a CDN.
"""
from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select, update
from telco_common.billing import Bill, data_usage_pct
from telco_common.catalog import COUNTRIES, get_plan
from telco_common.explainer import explain
from telco_common.flags import flag_bool

from ..db import get_engine, notifications, subscribers
from ..deps import RateLimiter, get_usage_state, get_valkey
from ..security import Principal, current_principal
from ..services.ai import Narrator
from ..services.bills import get_bill_row, list_bills, previous_cycle, row_to_bill, unbilled
from ..services.pdf import render_bill_pdf
from ..settings import get_settings

router = APIRouter(prefix="/v1/me", tags=["me"])
NO_STORE = {"Cache-Control": "private, no-store"}


def _subscriber(p: Principal):
    with get_engine().connect() as cx:
        row = cx.execute(select(subscribers).where(subscribers.c.id == p.subscriber_id)).first()
    if row is None:
        raise HTTPException(404, "subscriber not found")
    return row


def _bill_summary(row) -> dict:
    return {"id": row.id, "cycle": row.cycle, "total": row.total, "subtotal": row.subtotal, "vat": row.vat,
            "currency": row.currency, "status": row.status, "amount_paid": row.amount_paid,
            "outstanding": max(0, row.total - row.amount_paid), "issued_at": row.issued_at.isoformat(),
            "due_date": row.due_date.isoformat()}


@router.get("/home")
def home(response: Response, p: Principal = Depends(current_principal)) -> dict:
    response.headers.update(NO_STORE)
    sub = _subscriber(p)
    plan = get_plan(sub.plan_code)
    state = get_usage_state()
    now = datetime.now(UTC)
    current = unbilled(state, sub.id, sub.plan_code, now)
    counters = state.counters(sub.id, current.cycle)
    history = list_bills(get_engine(), sub.id, 12)
    last = history[-1] if history else None
    with get_engine().connect() as cx:
        notes = cx.execute(select(notifications).where(notifications.c.subscriber_id == sub.id)
                           .order_by(notifications.c.created_at.desc()).limit(10)).all()
    outstanding = sum(max(0, b.total - b.amount_paid) for b in history)
    return {
        "as_of": now.isoformat(),
        "subscriber": {"id": sub.id, "name": sub.name, "country": sub.country,
                       "locale": COUNTRIES[plan.country].locale},
        "plan": {"code": plan.code, "name": plan.name, "monthly_fee": plan.monthly_fee, "data_mb": plan.data_mb,
                 "voice_min": plan.voice_min, "sms": plan.sms, "overage_policy": plan.overage_policy},
        "usage": {"cycle": current.cycle, "data_pct": round(data_usage_pct(plan, counters), 1),
                  "version": state.committed_offset_for(sub.id), **current.usage.model_dump()},
        "unbilled": {"cycle": current.cycle, "currency": current.currency, "subtotal": current.subtotal,
                     "vat": current.vat, "total": current.total, "lines": [x.model_dump() for x in current.lines]},
        "outstanding": {"amount": outstanding, "currency": current.currency,
                        "bill_id": next((b.id for b in reversed(history) if b.total > b.amount_paid), None)},
        "last_bill": _bill_summary(last) if last else None,
        "trend": [_bill_summary(b) for b in history],
        "notifications": [{"id": n.id, "kind": n.kind, "title": n.title, "body": n.body,
                           "created_at": n.created_at.isoformat(), "read": n.read_at is not None} for n in notes],
        "features": {"ai_explanations": flag_bool("ai-explanations", sub.country, sub.id),
                     "payments": flag_bool("payments-enabled", sub.country, sub.id)},
    }


@router.get("/bills")
def bills(response: Response, months: int = Query(12, ge=1, le=24), p: Principal = Depends(current_principal)):
    response.headers.update(NO_STORE)
    return [_bill_summary(b) for b in list_bills(get_engine(), p.subscriber_id, months)]


def _load(p: Principal, cycle: str) -> tuple:
    row = get_bill_row(get_engine(), p.subscriber_id, cycle)
    if row is None:
        raise HTTPException(404, "bill not found")
    return row, row_to_bill(row)


@router.get("/bills/{cycle}")
def bill_detail(cycle: str, response: Response, p: Principal = Depends(current_principal)) -> dict:
    response.headers.update(NO_STORE)
    row, bill = _load(p, cycle)
    return _bill_summary(row) | {"lines": [x.model_dump() for x in bill.lines], "usage": bill.usage.model_dump(),
                                 "vat_rate": bill.vat_rate, "plan_code": bill.plan_code}


@router.get("/bills/{cycle}/pdf")
def bill_pdf(cycle: str, p: Principal = Depends(current_principal)) -> Response:
    row, bill = _load(p, cycle)
    pdf = render_bill_pdf(bill, _subscriber(p).name, row.status, row.due_date.date().isoformat())
    return Response(pdf, media_type="application/pdf", headers=NO_STORE | {
        "Content-Disposition": f'attachment; filename="bill-{cycle}.pdf"'})


@router.get("/bills/{cycle}/explanation")
def bill_explanation(cycle: str, response: Response, lang: str = Query("en", pattern="^(en|de|pl)$"),
                     ai: bool = Query(True, description="false = deterministic facts only (fast path for SSR)"),
                     p: Principal = Depends(current_principal)) -> dict:
    response.headers.update(NO_STORE)
    if cycle == "current":
        sub = _subscriber(p)
        bill: Bill = unbilled(get_usage_state(), sub.id, sub.plan_code)
        prev_row = get_bill_row(get_engine(), p.subscriber_id, previous_cycle(bill.cycle))
    else:
        _, bill = _load(p, cycle)
        prev_row = get_bill_row(get_engine(), p.subscriber_id, previous_cycle(cycle))
    ex = explain(bill, row_to_bill(prev_row) if prev_row else None)
    s = get_settings()
    allow = ai and RateLimiter(get_valkey()).allow(f"ai:{p.subscriber_id}", s.ai_daily_limit_per_subscriber, 86400)
    text, provider = Narrator(s, get_engine()).narrate(ex, p.subscriber_id, p.country, lang, allow_llm=allow)
    return {"explanation": ex.model_dump(), "narrative": text, "provider": provider, "partial": cycle == "current"}


@router.post("/notifications/{nid}/read", status_code=204)
def mark_read(nid: int, p: Principal = Depends(current_principal)) -> Response:
    with get_engine().begin() as cx:
        cx.execute(update(notifications).where(notifications.c.id == nid,
                                               notifications.c.subscriber_id == p.subscriber_id)
                   .values(read_at=datetime.now(UTC)))
    return Response(status_code=204)
