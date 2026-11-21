"""AddonPurchaseWorkflow (Temporal): an approved order that must reach a slow legacy BSS.

  validate ─► submit to legacy BSS ─► wait for its signal (durable timer, survives restarts)
          │                              └─ no signal in time → poll BSS status (activity), keep waiting
          ├─ CONFIRMED → apply: publish PLAN_CHANGED through the real usage pipeline (idempotent record id)
          │              → complete order → notify customer
          └─ REJECTED / gave up → compensate: mark FAILED → notify customer
Activities are retried by Temporal with backoff; the workflow itself is deterministic and replayable,
so a worker or pod restart resumes exactly where it was.
"""
from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    import json
    import uuid
    from datetime import UTC, datetime

    import httpx
    from sqlalchemy import insert, select

TASK_QUEUE = "orders"
RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                    maximum_interval=timedelta(minutes=1), maximum_attempts=8)


# ------------------------------- activities -------------------------------
@activity.defn
async def validate_order(order_id: str) -> dict:
    from ..db import get_engine, orders

    with get_engine().connect() as cx:
        row = cx.execute(select(orders).where(orders.c.id == order_id)).one()
    if row.status not in ("APPROVED", "SUBMITTED"):
        raise ValueError(f"order {order_id} is {row.status}, not approved")
    return {"subscriber_id": row.subscriber_id, "addon_code": row.addon_code}


@activity.defn
async def submit_to_bss(order_id: str) -> str:
    """Hand the order to the legacy BSS (idempotent: one BSS order per order)."""
    from ..db import bss_orders, get_engine
    from .orders import set_status

    engine = get_engine()
    with engine.begin() as cx:
        existing = cx.execute(select(bss_orders.c.id).where(bss_orders.c.order_id == order_id)).scalar()
        if existing:
            return existing
        bid = f"BSS-{uuid.uuid4().hex[:10].upper()}"
        cx.execute(insert(bss_orders).values(id=bid, order_id=order_id, status="SUBMITTED"))
    set_status(engine, order_id, "SUBMITTED", "Waiting for the network system to confirm", bss_order_id=bid)
    return bid


@activity.defn
async def bss_status(bss_order_id: str) -> str:
    from ..db import bss_orders, get_engine

    with get_engine().connect() as cx:
        return cx.execute(select(bss_orders.c.status).where(bss_orders.c.id == bss_order_id)).scalar() or "UNKNOWN"


@activity.defn
async def apply_addon(order_id: str, subscriber_id: str, addon_code: str) -> str:
    """Publish the change as a PLAN_CHANGED record through the collector, exactly like the network does.
    record_id = order id → a retried activity is dropped as a duplicate by the collector."""
    import os

    from ..db import get_engine, subscribers

    with get_engine().connect() as cx:
        country = cx.execute(select(subscribers.c.country).where(subscribers.c.id == subscriber_id)).scalar_one()
    rec = {"record_id": f"order-{order_id}", "subscriber_id": subscriber_id, "country": country,
           "kind": "PLAN_CHANGED", "addon_code": addon_code, "event_time": datetime.now(UTC).isoformat()}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(os.environ.get("APP_COLLECTOR_URL", "http://usage-collector:8080") + "/v1/usage-batches",
                         headers={"Authorization": f"Bearer {os.environ['APP_SOURCE_TOKEN']}", "X-Source": "emu-mediation-01"},
                         json={"batch_id": f"emu-mediation-01:order:{order_id}", "source": "emu-mediation-01", "records": [rec]})
    r.raise_for_status()
    return r.json().get("status", "accepted")


@activity.defn
async def finish_order(order_id: str, status: str, detail: str) -> None:
    from ..db import get_engine, notifications, orders
    from ..deps import get_valkey
    from .orders import set_status

    engine = get_engine()
    set_status(engine, order_id, status, detail)
    with engine.connect() as cx:
        row = cx.execute(select(orders).where(orders.c.id == order_id)).one()
    title = "Your data add-on is active" if status == "COMPLETED" else "Your add-on order couldn't be completed"
    with engine.begin() as cx:
        exists = cx.execute(select(notifications.c.id).where(notifications.c.alert_id == f"order:{order_id}")).first()
        if not exists:
            cx.execute(insert(notifications).values(subscriber_id=row.subscriber_id, alert_id=f"order:{order_id}",
                                                    kind=f"ORDER_{status}", title=title, body=detail))
    from ..services.home_cache import bump

    bump(get_valkey(), row.subscriber_id)
    get_valkey().publish(f"notif:{row.subscriber_id}", json.dumps({"kind": f"ORDER_{status}", "title": title, "body": detail}))


ACTIVITIES = [validate_order, submit_to_bss, bss_status, apply_addon, finish_order]


# ------------------------------- workflow -------------------------------
@workflow.defn
class AddonPurchaseWorkflow:
    def __init__(self) -> None:
        self.bss_result: str | None = None
        self.stage = "starting"

    @workflow.signal
    def bss_callback(self, result: str) -> None:
        self.bss_result = result

    @workflow.query
    def progress(self) -> dict:
        return {"stage": self.stage, "bss_result": self.bss_result}

    @workflow.run
    async def run(self, order_id: str) -> str:
        opts = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": RETRY}
        self.stage = "validating"
        info = await workflow.execute_activity(validate_order, order_id, **opts)
        self.stage = "submitting to network system"
        bss_id = await workflow.execute_activity(submit_to_bss, order_id, **opts)

        self.stage = "waiting for network system"
        deadline = workflow.now() + timedelta(minutes=30)
        while self.bss_result is None and workflow.now() < deadline:
            try:  # durable wait: a worker restart resumes this timer from history
                await workflow.wait_condition(lambda: self.bss_result is not None, timeout=timedelta(seconds=45))
            except TimeoutError:
                status = await workflow.execute_activity(bss_status, bss_id, **opts)  # callback lost? poll
                if status in ("CONFIRMED", "REJECTED"):
                    self.bss_result = status

        if self.bss_result == "CONFIRMED":
            self.stage = "applying"
            await workflow.execute_activity(apply_addon, args=[order_id, info["subscriber_id"], info["addon_code"]], **opts)
            self.stage = "completed"
            await workflow.execute_activity(finish_order, args=[order_id, "COMPLETED",
                                            "The add-on is active: your data allowance for this cycle has grown."], **opts)
            return "COMPLETED"

        self.stage = "compensating"  # rejected or timed out: nothing was applied, tell the customer
        reason = "The network system rejected the order." if self.bss_result == "REJECTED" else \
            "The network system didn't confirm in time. You have not been charged."
        await workflow.execute_activity(finish_order, args=[order_id, "FAILED", reason], **opts)
        return "FAILED"
