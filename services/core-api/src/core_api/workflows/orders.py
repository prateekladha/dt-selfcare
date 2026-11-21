"""Order domain: proposals (from the app or the assistant) and their lifecycle."""
from __future__ import annotations

import uuid

from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine
from telco_common.catalog import ADDONS, COUNTRIES, get_plan

from ..db import orders, subscribers, utcnow


class OrderError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def public(row) -> dict:
    return {"order_id": row.id, "kind": row.kind, "addon_code": row.addon_code, "price": row.price,
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
    return public(row) | {"preview": {"name": addon.name, "data_mb": addon.data_mb, "price": addon.price,
                                      "currency": cur, "plan": plan.name}}


def set_status(engine: Engine, order_id: str, status: str, detail: str | None = None, **extra) -> None:
    values = {"status": status, "updated_at": utcnow(), **extra}
    if detail is not None:
        values["detail"] = detail[:500]
    with engine.begin() as cx:
        cx.execute(update(orders).where(orders.c.id == order_id).values(**values))


def get(engine: Engine, subscriber_id: str, order_id: str):
    with engine.connect() as cx:
        return cx.execute(select(orders).where(orders.c.id == order_id, orders.c.subscriber_id == subscriber_id)).first()
