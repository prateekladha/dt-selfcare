"""Customer-facing API. Every route is scoped to the token's subscriber.

GET /v1/me/home is the BFF call: one consistent snapshot for the whole home screen, stamped with
`as_of` and per-widget `version`s, so the SSR page never renders widgets from different moments.
Responses carry `Cache-Control: private, no-store`: personalised data is never cached by a CDN.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy import select, update
from telco_common.billing import Bill, data_usage_pct
from telco_common.catalog import COUNTRIES, get_plan
from telco_common.explainer import explain
from telco_common.flags import flag_bool
from telco_common.models import cycle_of

from ..db import get_engine, notifications, subscribers
from ..deps import get_usage_state, get_valkey
from ..security import Principal, current_principal
from ..services.bills import get_bill_row, list_bills, previous_cycle, row_to_bill, unbilled
from ..services.pdf import render_bill_pdf

# a cycle in a path is a month, or the aliases current/latest: anything else is rejected before any work
# (an arbitrary string would miss every cache and, for evidence, force a full archive re-scan)
CYCLE = Path(pattern=r"^(\d{4}-(0[1-9]|1[0-2])|current|latest)$")

router = APIRouter(prefix="/v1/me", tags=["me"])
NO_STORE = {"Cache-Control": "private, no-store"}


def _subscriber(p: Principal):
    with get_engine().connect() as cx:
        row = cx.execute(select(subscribers).where(subscribers.c.id == p.subscriber_id)).first()
    if row is None:
        raise HTTPException(404, "subscriber not found")
    return row


def _bill_summary(row) -> dict:
    from ..services.bills import bill_summary

    return bill_summary(row)


@router.get("/home")
def home(response: Response, p: Principal = Depends(current_principal)) -> dict:
    """One call builds the whole home screen (BFF-style aggregate). Profile, bills and notifications come
    from the event-invalidated cache (hit: zero DB queries); live usage/unbilled from the usage state."""
    from ..services.home_cache import build_snapshot, get_or_build

    response.headers.update(NO_STORE)

    def build() -> dict:
        try:
            return build_snapshot(get_engine(), p.subscriber_id)
        except LookupError as e:
            raise HTTPException(404, "subscriber not found") from e

    snap, hit = get_or_build(get_valkey(), p.subscriber_id, build)
    response.headers["X-Home-Cache"] = "hit" if hit else "miss"
    sub, history = snap["sub"], snap["bills"]
    plan = get_plan(sub["plan_code"])
    state = get_usage_state()
    now = datetime.now(UTC)
    seg = [(get_plan(c), d) for c, d in snap["plan_segments"]] if snap.get("plan_segments") else None
    current = unbilled(state, sub["id"], sub["plan_code"], now, seg=seg)
    counters = state.counters(sub["id"], current.cycle)
    last = history[-1] if history else None
    return {
        "as_of": now.isoformat(),
        "subscriber": {"id": sub["id"], "name": sub["name"], "email": sub["email"], "country": sub["country"],
                       "locale": COUNTRIES[plan.country].locale},
        "plan": {"code": plan.code, "name": plan.name, "monthly_fee": plan.monthly_fee, "data_mb": plan.data_mb,
                 "voice_min": plan.voice_min, "sms": plan.sms, "overage_policy": plan.overage_policy},
        "usage": {"cycle": current.cycle, "data_pct": round(data_usage_pct(plan, counters), 1),
                  "version": state.committed_offset_for(sub["id"]), **current.usage.model_dump()},
        "unbilled": {"cycle": current.cycle, "currency": current.currency, "subtotal": current.subtotal,
                     "vat": current.vat, "total": current.total, "lines": [x.model_dump() for x in current.lines]},
        # the OLDEST unpaid bill comes first (it's the one that is or becomes overdue); the amount covers all
        "outstanding": {"amount": sum(b["outstanding"] for b in history), "currency": current.currency,
                        "bill_id": next((b["id"] for b in history if b["outstanding"] > 0), None),
                        "unpaid_bills": sum(1 for b in history if b["outstanding"] > 0)},
        "last_bill": last,
        "trend": history,
        "notifications": snap["notes"],
        "features": {"ai_explanations": flag_bool("ai-explanations", sub["country"], sub["id"]),
                     "payments": flag_bool("payments-enabled", sub["country"], sub["id"])},
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
def bill_detail(cycle: Annotated[str, CYCLE], response: Response, p: Principal = Depends(current_principal)) -> dict:
    response.headers.update(NO_STORE)
    row, bill = _load(p, cycle)
    return _bill_summary(row) | {"lines": [x.model_dump() for x in bill.lines], "usage": bill.usage.model_dump(),
                                 "vat_rate": bill.vat_rate, "plan_code": bill.plan_code}


@router.get("/bills/{cycle}/pdf")
def bill_pdf(cycle: Annotated[str, CYCLE], p: Principal = Depends(current_principal)) -> Response:
    from ..db import bill_payments, payments
    from ..services.pdf import InvoiceContext, PaymentEntry

    row, bill = _load(p, cycle)
    sub = _subscriber(p)
    prev_row = get_bill_row(get_engine(), p.subscriber_id, previous_cycle(cycle))
    with get_engine().connect() as cx:  # payments applied to this bill (billing ledger) + gateway details
        paid = cx.execute(
            select(bill_payments.c.amount, bill_payments.c.applied_at, payments.c.gateway,
                   payments.c.gateway_payment_id, bill_payments.c.payment_id)
            .select_from(bill_payments.outerjoin(payments, payments.c.id == bill_payments.c.payment_id))
            .where(bill_payments.c.bill_id == row.id).order_by(bill_payments.c.applied_at)).all()
    ctx = InvoiceContext(
        bill=bill, invoice_number=row.id, issued_at=row.issued_at, due_date=row.due_date,
        amount_paid=row.amount_paid, customer_name=sub.name, msisdn=sub.id, country=sub.country, email=sub.email,
        previous_bill=row_to_bill(prev_row) if prev_row else None,
        payments=[PaymentEntry(x.amount, x.applied_at, f"In-app ({x.gateway})" if x.gateway else "In-app",
                               x.gateway_payment_id or x.payment_id) for x in paid])
    pdf = render_bill_pdf(ctx)
    return Response(pdf, media_type="application/pdf", headers=NO_STORE | {
        "Content-Disposition": f'attachment; filename="bill-{cycle}.pdf"'})


@router.get("/bills/{cycle}/explanation")
def bill_explanation(cycle: Annotated[str, CYCLE], response: Response, lang: str = Query("en", pattern="^(en|de|pl)$"),
                     ai: bool = Query(True, description="false = deterministic facts only (fast path for SSR)"),
                     p: Principal = Depends(current_principal)) -> dict:
    response.headers.update(NO_STORE)
    if cycle == "latest":  # lets the home screen ask in one call instead of list-then-fetch
        last = list_bills(get_engine(), p.subscriber_id, 1)
        if not last:
            raise HTTPException(404, "no bills yet")
        cycle = last[-1].cycle
    if cycle == "current":
        sub = _subscriber(p)
        bill: Bill = unbilled(get_usage_state(), sub.id, sub.plan_code)
        prev_row = get_bill_row(get_engine(), p.subscriber_id, previous_cycle(bill.cycle))
    else:
        _, bill = _load(p, cycle)
        prev_row = get_bill_row(get_engine(), p.subscriber_id, previous_cycle(cycle))
    ex = explain(bill, row_to_bill(prev_row) if prev_row else None)
    out = {"explanation": ex.model_dump(), "partial": cycle == "current",
           "bill_id": None if cycle == "current" else f"{p.subscriber_id}:{cycle}"}
    if not ai:
        return out | {"llm": None, "status": "SKIPPED"}
    from ..agent.explain_llm import explain_bill
    from ..agent.factory import build_llm

    if not flag_bool("ai-explanations", p.country, p.subscriber_id):
        return out | {"llm": None, "status": "UNAVAILABLE", "reason": "switched off"}
    try:
        import hashlib

        llm = build_llm(p.country, p.subscriber_id)
        llm.meta = {"trace_name": "bill-explanation", "trace_user_id": hashlib.sha256(p.subscriber_id.encode()).hexdigest()[:16],
                    "cycle": bill.cycle}
        res = explain_bill(llm, get_valkey(), bill,
                           row_to_bill(prev_row) if prev_row else None, lang)
    except RuntimeError as e:
        return out | {"llm": None, "status": "UNAVAILABLE", "reason": str(e)}
    except Exception as e:  # noqa: BLE001 - provider outage: show the bill, say the explanation is unavailable
        return out | {"llm": None, "status": "UNAVAILABLE", "reason": type(e).__name__}
    return out | {"llm": res["explanation"], "status": res["status"], "sources": res["sources"], "trace": res["trace"]}


@router.get("/bills/{cycle}/evidence")
def bill_evidence(cycle: Annotated[str, CYCLE], response: Response, p: Principal = Depends(current_principal)) -> dict:
    """Raw-CDR evidence for a bill: re-summed from the archive and reconciled with Valkey + the bill."""
    from ..services.evidence import cdr_evidence

    response.headers.update(NO_STORE)
    from ..deps import RateLimiter, get_valkey

    if not RateLimiter(get_valkey()).allow(f"evidence:{p.subscriber_id}", 30, 3600):  # each cold scan reads the archive
        raise HTTPException(429, "You've checked your records many times this hour. Please try again later.")
    real_cycle = cycle_of(datetime.now(UTC)) if cycle == "current" else cycle
    ev = cdr_evidence(get_usage_state(), p.subscriber_id, real_cycle)
    row = None if cycle == "current" else get_bill_row(get_engine(), p.subscriber_id, cycle)
    ev["bill_lines"] = [x.model_dump() for x in row_to_bill(row).lines] if row else []
    ev["bill_id"] = row.id if row else None
    ev["note"] = ("This month: the audit copy is written about once a minute, so the newest records may not be in it yet."
                  if cycle == "current" else None)
    return ev


@router.post("/notifications/{nid}/read", status_code=204)
def mark_read(nid: int, p: Principal = Depends(current_principal)) -> Response:
    with get_engine().begin() as cx:
        cx.execute(update(notifications).where(notifications.c.id == nid,
                                               notifications.c.subscriber_id == p.subscriber_id)
                   .values(read_at=datetime.now(UTC)))
    from ..services.home_cache import bump_default

    bump_default(p.subscriber_id)
    return Response(status_code=204)
