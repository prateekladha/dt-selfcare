"""Order domain: proposals (from the app or the assistant) and their lifecycle.

Every status change is written together with an ORDER_STATUS_CHANGED row in the outbox, in the SAME
transaction, and relayed to the `order-events` stream: consumers (notifications, home-screen cache,
assistant context) can never miss a change or see one that was rolled back.
"""
from __future__ import annotations

import json
import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from telco_common.catalog import ADDONS, COUNTRIES, PLANS, get_plan

from ..db import orders, outbox, subscribers, utcnow


class OrderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _event(cx, row) -> None:
    """ORDER_STATUS_CHANGED into the outbox, inside the caller's transaction."""
    cx.execute(outbox.insert().values(topic="order-events", event_key=row.subscriber_id, payload=json.dumps({
        "type": "ORDER_STATUS_CHANGED", "order_id": row.id, "subscriber_id": row.subscriber_id, "kind": row.kind,
        "offering": row.addon_code, "status": row.status, "detail": row.detail,
        "at": (row.updated_at or row.created_at).isoformat()})))


def public(row) -> dict:
    return {"order_id": row.id, "kind": row.kind, "addon_code": row.addon_code, "offering": row.addon_code, "price": row.price,
            "currency": row.currency, "status": row.status, "detail": row.detail, "source": row.source,
            "created_at": row.created_at.isoformat(), "updated_at": row.updated_at.isoformat()}


def propose_addon(engine: Engine, subscriber_id: str, addon_code: str, source: str) -> dict:
    """Creates a PROPOSED order with a preview. No side effect on the customer's plan until approved."""
    with engine.connect() as cx:
        sub = cx.execute(select(subscribers).where(subscribers.c.id == subscriber_id)).one()
    plan = get_plan(sub.plan_code)
    addon = ADDONS.get(addon_code)
    if addon is None or addon.country != plan.country:
        raise OrderError(422, f"add-on {addon_code!r} is not available for this plan")
    oid = str(uuid.uuid4())
    cur = COUNTRIES[plan.country].currency
    with engine.begin() as cx:
        cx.execute(insert(orders).values(id=oid, subscriber_id=subscriber_id, kind="ADDON", addon_code=addon.code,
                                         price=addon.price, currency=cur, status="PROPOSED", source=source,
                                         detail=f"{addon.name}: +{addon.data_mb:,} MB this cycle"))
        row = cx.execute(select(orders).where(orders.c.id == oid)).one()
        _event(cx, row)
    return public(row) | {"preview": {"name": addon.name, "data_mb": addon.data_mb, "price": addon.price,
                                      "currency": cur, "plan": plan.name}}


def propose_plan_change(engine: Engine, subscriber_id: str, plan_code: str, source: str) -> dict:
    """PROPOSED tariff change with a side-by-side preview. Nothing changes until approved."""
    with engine.connect() as cx:
        sub = cx.execute(select(subscribers).where(subscribers.c.id == subscriber_id)).one()
    cur_plan = get_plan(sub.plan_code)
    new = PLANS.get(plan_code)
    if new is None or new.country != cur_plan.country:
        raise OrderError(422, f"tariff {plan_code!r} is not available in this market")
    if new.code == cur_plan.code:
        raise OrderError(409, "that is already your tariff")
    oid = str(uuid.uuid4())
    cur = COUNTRIES[new.country].currency
    gb = lambda mb: "unlimited" if mb < 0 else f"{mb / 1000:g} GB"
    with engine.begin() as cx:
        cx.execute(insert(orders).values(id=oid, subscriber_id=subscriber_id, kind="PLAN", addon_code=new.code,
                                         price=new.monthly_fee, currency=cur, status="PROPOSED", source=source,
                                         detail=f"Switch from {cur_plan.name} to {new.name}"))
        row = cx.execute(select(orders).where(orders.c.id == oid)).one()
        _event(cx, row)
    return public(row) | {"preview": {"name": new.name, "from": cur_plan.name, "monthly_fee": new.monthly_fee,
                                      "old_monthly_fee": cur_plan.monthly_fee, "data": gb(new.data_mb),
                                      "old_data": gb(cur_plan.data_mb), "currency": cur, "plan": cur_plan.name}}


def set_status(engine: Engine, order_id: str, status: str, detail: str | None = None, **extra) -> None:
    values = {"status": status, "updated_at": utcnow(), **extra}
    if detail is not None:
        values["detail"] = detail[:500]
    with engine.begin() as cx:
        cx.execute(update(orders).where(orders.c.id == order_id).values(**values))
        row = cx.execute(select(orders).where(orders.c.id == order_id)).first()
        if row is not None:
            _event(cx, row)


def get(engine: Engine, subscriber_id: str, order_id: str):
    with engine.connect() as cx:
        return cx.execute(select(orders).where(orders.c.id == order_id, orders.c.subscriber_id == subscriber_id)).first()
