"""Write actions with human-in-the-loop: propose -> customer approves -> durable Temporal workflow."""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db import get_engine, orders
from ..security import Principal, current_principal
from ..workflows.orders import (
    OrderError,
    _credit_check,
    expired,
    get,
    propose_addon,
    propose_plan_change,
    public,
    set_status,
)

router = APIRouter(prefix="/v1/me/orders", tags=["orders"])
_client = None


async def temporal():
    global _client
    if _client is None:
        from temporalio.client import Client

        _client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"),
                                       namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"))
    return _client


class AddonIn(BaseModel):
    addon_code: str = Field(min_length=3, max_length=32)


@router.post("/addon", status_code=201)
def propose(body: AddonIn, p: Principal = Depends(current_principal)) -> dict:
    try:
        return propose_addon(get_engine(), p.subscriber_id, body.addon_code, source="app")
    except OrderError as e:
        raise HTTPException(e.status, e.message) from e


class PlanIn(BaseModel):
    plan_code: str = Field(min_length=3, max_length=32)


@router.post("/plan", status_code=201)
def propose_plan(body: PlanIn, p: Principal = Depends(current_principal)) -> dict:
    try:
        return propose_plan_change(get_engine(), p.subscriber_id, body.plan_code, source="app")
    except OrderError as e:
        raise HTTPException(e.status, e.message) from e


@router.get("/plans/available")
def available_plans(p: Principal = Depends(current_principal)) -> dict:
    from telco_common.catalog import COUNTRIES, plans_for

    from ..db import subscribers

    with get_engine().connect() as cx:
        sub = cx.execute(select(subscribers).where(subscribers.c.id == p.subscriber_id)).one()
    return {"current": sub.plan_code, "currency": COUNTRIES[sub.country].currency, "plans": [
        {"code": x.code, "name": x.name, "monthly_fee": x.monthly_fee, "data_mb": x.data_mb, "voice_min": x.voice_min,
         "sms": x.sms, "overage_policy": x.overage_policy} for x in plans_for(sub.country)]}


@router.post("/{order_id}/approve")
async def approve(order_id: str, p: Principal = Depends(current_principal)) -> dict:
    """The customer's approval starts the workflow. Workflow id = order id, so a double click can't
    start two workflows (Temporal rejects a duplicate id)."""
    from temporalio.exceptions import WorkflowAlreadyStartedError

    from ..workflows.addon import TASK_QUEUE, AddonPurchaseWorkflow, PlanChangeWorkflow

    row = get(get_engine(), p.subscriber_id, order_id)
    if row is None:
        raise HTTPException(404, "order not found")
    if expired(row):
        raise HTTPException(410, "This offer has expired. Ask again for a fresh one with today's price.")
    if row.status not in ("PROPOSED", "APPROVED", "DECLINED"):  # DECLINED: the customer changed their mind
        return public(row)
    try:
        _credit_check(get_engine(), p.subscriber_id)
    except OrderError as e:
        raise HTTPException(e.status, e.message) from e
    previous = row.status
    if previous != "APPROVED" and not set_status(get_engine(), order_id, "APPROVED", "Approved: processing",
                                                  expect=("PROPOSED", "DECLINED")):
        return public(get(get_engine(), p.subscriber_id, order_id))   # someone else changed it meanwhile
    try:
        from temporalio.common import WorkflowIDReusePolicy

        wf = PlanChangeWorkflow.run if row.kind == "PLAN" else AddonPurchaseWorkflow.run
        await (await temporal()).start_workflow(wf, order_id, id=order_id, task_queue=TASK_QUEUE,
                                                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
    except WorkflowAlreadyStartedError:
        pass
    except Exception as e:  # workflow service unavailable: undo, so the order isn't "processing" forever
        set_status(get_engine(), order_id, previous, "Not processed yet: please approve again in a moment",
                   expect=("APPROVED",))
        raise HTTPException(503, "We couldn't start your order right now. Nothing was charged; please try again.") from e
    return public(get(get_engine(), p.subscriber_id, order_id))


@router.post("/{order_id}/decline")
def decline(order_id: str, p: Principal = Depends(current_principal)) -> dict:
    row = get(get_engine(), p.subscriber_id, order_id)
    if row is None:
        raise HTTPException(404, "order not found")
    if row.status == "PROPOSED":   # only from PROPOSED, atomically: an approval that won the race stands
        set_status(get_engine(), order_id, "DECLINED", "Declined by the customer", expect=("PROPOSED",))
    return public(get(get_engine(), p.subscriber_id, order_id))


@router.get("/{order_id}")
async def status(order_id: str, p: Principal = Depends(current_principal)) -> dict:
    row = get(get_engine(), p.subscriber_id, order_id)
    if row is None:
        raise HTTPException(404, "order not found")
    out = public(row)
    if row.status in ("APPROVED", "SUBMITTED"):
        try:
            out["workflow"] = await (await temporal()).get_workflow_handle(order_id).query("progress")
        except Exception:  # noqa: BLE001 - workflow status is informative only
            out["workflow"] = None
    return out


@router.get("")
def list_orders(p: Principal = Depends(current_principal)) -> list[dict]:
    with get_engine().connect() as cx:
        rows = cx.execute(select(orders).where(orders.c.subscriber_id == p.subscriber_id)
                          .order_by(orders.c.created_at.desc()).limit(20)).all()
    return [public(r) for r in rows]
