"""Order workflows (Temporal): an approved order that must reach a slow legacy order system.

  validate ─► submit TMF622 ProductOrder to the order-adapter service (never to a legacy system)
          │      └─ 429 "legacy at capacity" / timeout / circuit open → activity retry with backoff:
          │         a bill-day spike waits durably in Temporal instead of overloading the legacy system
          ├─ wait for the legacy result: `bss-events` stream → worker → signal (durable timer survives restarts)
          │      └─ no signal in time → ask the adapter for the TMF622 state, keep waiting
          ├─ completed → apply  (add-on: PLAN_CHANGED through the usage pipeline; tariff: switch the plan)
          │            → order COMPLETED (status change goes out on `order-events` from the same DB transaction)
          └─ failed / gave up → compensate: order FAILED (nothing applied)
Notifications, the home-screen cache and the assistant's context are updated by consumers of
`order-events`, not by the workflow. Activities are retried by Temporal; workflows are deterministic.

Two workflows share these activities and the same adapters:
  AddonPurchaseWorkflow - buy a data add-on;  PlanChangeWorkflow - switch tariff.
"""
from __future__ import annotations

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    import os
    from datetime import UTC, datetime

    import httpx
    from sqlalchemy import select, update

TASK_QUEUE = "orders"


def legacy_queue(country: str) -> str:
    """One task queue per country's legacy order system. Its workers are rate-limited by the Temporal
    server to that system's capacity (across all worker pods), so on a spike the calls WAIT in this
    queue and are dispatched at the capacity rate - no 429 churn, no growing backoff."""
    return f"legacy-bss-{country}"
RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                    maximum_interval=timedelta(minutes=1), maximum_attempts=8)
# submitting to a legacy system that may be at capacity on bill day: keep backing off for hours
# After the network CONFIRMED an order, applying it must eventually happen: retry without an attempt cap
# (a capped retry used to fail the workflow and leave the order SUBMITTED forever, confirmed but never applied).
APPLY_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                          maximum_interval=timedelta(minutes=2), maximum_attempts=0)
SUBMIT_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                           maximum_interval=timedelta(minutes=5), non_retryable_error_types=["UnsupportedMarket"])

_adapter = None
QUEUED_TEXT = ("Queued: the network system is busy right now. We'll complete this automatically and notify you. "
               "You can leave this page.")


def adapter_client():
    """order-adapter with connect/read timeouts and a circuit breaker (shared per worker process)."""
    global _adapter
    if _adapter is None:
        from telco_common.resilience import ServiceClient

        _adapter = ServiceClient("order-adapter", os.environ.get("ORDER_ADAPTER_URL", "http://order-adapter:8080"),
                                 connect_s=1.0, read_s=5.0, fail_threshold=5, reset_after_s=30)
    return _adapter


# ------------------------------- activities -------------------------------
@activity.defn
async def validate_order(order_id: str) -> dict:
    from ..db import get_engine, orders

    with get_engine().connect() as cx:
        row = cx.execute(select(orders).where(orders.c.id == order_id)).one()
    if row.status not in ("APPROVED", "SUBMITTED"):
        raise ValueError(f"order {order_id} is {row.status}, not approved")
    from ..db import subscribers

    with get_engine().connect() as cx:
        country = cx.execute(select(subscribers.c.country).where(subscribers.c.id == row.subscriber_id)).scalar_one()
    return {"subscriber_id": row.subscriber_id, "addon_code": row.addon_code, "kind": row.kind, "country": country}


@activity.defn
async def submit_to_bss(order_id: str) -> str:
    """POST a TMF622 ProductOrder to the order-adapter. Idempotent (one legacy order per order)."""
    from temporalio.exceptions import ApplicationError

    from ..adapters.bss import BASE, product_order
    from ..db import get_engine, orders, subscribers
    from .orders import set_status

    engine = get_engine()
    with engine.connect() as cx:
        o = cx.execute(select(orders).where(orders.c.id == order_id)).one()
        country = cx.execute(select(subscribers.c.country).where(subscribers.c.id == o.subscriber_id)).scalar_one()
    body = product_order(order_id, o.subscriber_id, o.addon_code, country, "add" if o.kind == "ADDON" else "modify")
    r = await adapter_client().request("POST", BASE, json=body)
    if r.status_code == 429:  # legacy at capacity: Temporal backs off and retries
        if activity.info().attempt == 1:  # tell the customer once (their card shows it; emits an order event)
            set_status(engine, order_id, "APPROVED", QUEUED_TEXT, expect=("APPROVED",))   # never regress a later state
        raise ApplicationError("legacy order system at capacity", type="AtCapacity",
                               next_retry_delay=timedelta(seconds=int(r.headers.get("Retry-After", "1")) + 1))
    if r.status_code == 422:
        raise ApplicationError(r.text[:200], type="UnsupportedMarket", non_retryable=True)
    r.raise_for_status()
    bid = r.json()["id"]
    set_status(engine, order_id, "SUBMITTED", "Waiting for the network system to confirm", bss_order_id=bid,
               expect=("APPROVED", "SUBMITTED"))
    return bid


@activity.defn
async def bss_status(bss_order_id: str) -> str:
    """TMF622 state of the legacy order, via the adapter (used only when the event didn't arrive)."""
    from ..adapters.bss import BASE

    r = await adapter_client().request("GET", f"{BASE}/{bss_order_id}")
    r.raise_for_status()
    return r.json()["state"]


async def _publish_record(order_id: str, rec: dict) -> str:
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=1.0, read=10.0, write=10.0, pool=1.0)) as c:
        r = await c.post(os.environ.get("APP_COLLECTOR_URL", "http://usage-collector:8080") + "/v1/usage-batches",
                         headers={"Authorization": f"Bearer {os.environ['APP_SOURCE_TOKEN']}", "X-Source": "emu-mediation-01"},
                         json={"batch_id": f"emu-mediation-01:order:{order_id}", "source": "emu-mediation-01", "records": [rec]})
    r.raise_for_status()
    return r.json().get("status", "accepted")


@activity.defn
async def apply_addon(order_id: str, subscriber_id: str, addon_code: str) -> str:
    """Publish the change as a PLAN_CHANGED record through the collector, exactly like the network does.
    record_id = order id → a retried activity is dropped as a duplicate by the collector."""
    from ..db import get_engine, subscribers

    with get_engine().connect() as cx:
        country = cx.execute(select(subscribers.c.country).where(subscribers.c.id == subscriber_id)).scalar_one()
    return await _publish_record(order_id, {
        "record_id": f"order-{order_id}", "subscriber_id": subscriber_id, "country": country,
        "kind": "PLAN_CHANGED", "addon_code": addon_code, "event_time": datetime.now(UTC).isoformat()})


@activity.defn
async def apply_plan(order_id: str, subscriber_id: str, plan_code: str) -> str:
    """Switch the tariff and record it in the tariff history in ONE transaction (the history drives
    pro-rating). Idempotent: the history row is keyed by order id, so a retried activity changes nothing."""
    from sqlalchemy.exc import IntegrityError

    from ..db import get_engine, plan_changes, subscribers

    try:
        with get_engine().begin() as cx:
            old = cx.execute(select(subscribers.c.plan_code).where(subscribers.c.id == subscriber_id)).scalar_one()
            cx.execute(plan_changes.insert().values(order_id=order_id, subscriber_id=subscriber_id, from_plan=old,
                                                    to_plan=plan_code, effective_at=datetime.now(UTC)))
            cx.execute(update(subscribers).where(subscribers.c.id == subscriber_id).values(plan_code=plan_code))
    except IntegrityError:
        pass  # already applied by an earlier attempt
    # The real-time pipeline computes alerts from its own profile copy: without this, an upgrade kept the old
    # allowance (false "data used up" alerts) for good. A bigger allowance re-arms the levels now below.
    from telco_common.models import cycle_of

    from ..deps import get_usage_state

    state = get_usage_state()
    before = int(state.profile(subscriber_id).get("allowance_bytes", 0) or 0)
    state.upsert_profile(subscriber_id, plan_code)
    after = int(state.profile(subscriber_id).get("allowance_bytes", 0) or 0)
    if after > before:
        state.rearm_alerts(subscriber_id, cycle_of(datetime.now(UTC)))
    return plan_code


@activity.defn
async def finish_order(order_id: str, status: str, detail: str) -> None:
    """Terminal status. The status change is emitted on `order-events` in the same transaction; the
    notification, home cache and assistant context are updated by that stream's consumers."""
    from ..db import get_engine
    from .orders import set_status

    # terminal once: a retried finish can't emit a second event, a late write can't undo it
    set_status(get_engine(), order_id, status, detail, expect=("APPROVED", "SUBMITTED"))


ACTIVITIES = [validate_order, apply_addon, apply_plan, finish_order]      # task queue "orders"
LEGACY_ACTIVITIES = [submit_to_bss, bss_status]                            # task queues "legacy-bss-<country>"
TMF_TO_RESULT = {"completed": "CONFIRMED", "failed": "REJECTED"}


# ------------------------------- workflows -------------------------------
class _LegacyOrder:
    """Shared shape: submit via the adapter, wait for the legacy result (event or poll), then apply or compensate."""

    def __init__(self) -> None:
        self.bss_result: str | None = None
        self.stage = "starting"
        self.failure: str | None = None

    async def _submit_and_wait(self, order_id: str) -> tuple[dict, str | None]:
        """-> (order info, CONFIRMED | REJECTED | None). Never raises for an activity that ran out of retries:
        before the network confirmed, such a failure ends the order as FAILED (not charged) instead of leaving
        it APPROVED/SUBMITTED forever with no notification."""
        opts = {"start_to_close_timeout": timedelta(seconds=30), "retry_policy": RETRY}
        self.stage = "validating"
        try:
            info = await workflow.execute_activity(validate_order, order_id, **opts)
            legacy = legacy_queue(info.get("country", "DE"))
            self.stage = "in line for the network system"   # waits in the country's rate-limited task queue
            bss_id = await workflow.execute_activity(submit_to_bss, order_id, task_queue=legacy,
                                                     start_to_close_timeout=timedelta(seconds=30),
                                                     schedule_to_close_timeout=timedelta(hours=12), retry_policy=SUBMIT_RETRY)
        except ActivityError:
            self.failure = "not_submitted"
            return {}, None
        self.stage = "waiting for network system"
        deadline = workflow.now() + timedelta(minutes=30)

        async def ask() -> None:   # event lost / late? ask the adapter; an adapter outage just means "ask again"
            try:
                state = await workflow.execute_activity(bss_status, bss_id, task_queue=legacy, **opts)
            except ActivityError:
                return
            if state in TMF_TO_RESULT and self.bss_result is None:
                self.bss_result = TMF_TO_RESULT[state]

        while self.bss_result is None and workflow.now() < deadline:
            try:  # durable wait for the bss-events signal; a worker restart resumes this timer from history
                await workflow.wait_condition(lambda: self.bss_result is not None, timeout=timedelta(seconds=45))
            except TimeoutError:
                await ask()
        if self.bss_result is None:
            await ask()   # one last check before giving up, so a just-confirmed order isn't reported as failed
        return info, self.bss_result

    async def _finish(self, order_id: str, ok: bool, done_text: str) -> str:
        if ok:
            self.stage = "completed"
            await workflow.execute_activity(finish_order, args=[order_id, "COMPLETED", done_text],
                                            start_to_close_timeout=timedelta(seconds=30), retry_policy=APPLY_RETRY)
            return "COMPLETED"
        self.stage = "compensating"  # rejected or timed out: nothing was applied
        reason = ("The network system rejected the order. You have not been charged." if self.bss_result == "REJECTED"
                  else "We couldn't process the order right now. You have not been charged; please try again later."
                  if self.failure else "The network system didn't confirm in time. You have not been charged.")
        await workflow.execute_activity(finish_order, args=[order_id, "FAILED", reason],
                                        start_to_close_timeout=timedelta(seconds=30), retry_policy=APPLY_RETRY)
        return "FAILED"


@workflow.defn
class AddonPurchaseWorkflow(_LegacyOrder):
    @workflow.signal
    def bss_callback(self, result: str) -> None:
        if self.bss_result is None:   # first result wins: a later, conflicting callback can't flip the outcome
            self.bss_result = result

    @workflow.query
    def progress(self) -> dict:
        return {"stage": self.stage, "bss_result": self.bss_result}

    @workflow.run
    async def run(self, order_id: str) -> str:
        info, result = await self._submit_and_wait(order_id)
        if result == "CONFIRMED":
            self.stage = "applying"
            await workflow.execute_activity(apply_addon, args=[order_id, info["subscriber_id"], info["addon_code"]],
                                            start_to_close_timeout=timedelta(seconds=30), retry_policy=APPLY_RETRY)
        return await self._finish(order_id, result == "CONFIRMED",
                                  "The add-on is active: your data allowance for this cycle has grown.")


@workflow.defn
class PlanChangeWorkflow(_LegacyOrder):
    @workflow.signal
    def bss_callback(self, result: str) -> None:
        if self.bss_result is None:   # first result wins: a later, conflicting callback can't flip the outcome
            self.bss_result = result

    @workflow.query
    def progress(self) -> dict:
        return {"stage": self.stage, "bss_result": self.bss_result}

    @workflow.run
    async def run(self, order_id: str) -> str:
        info, result = await self._submit_and_wait(order_id)
        if result == "CONFIRMED":
            self.stage = "switching tariff"
            await workflow.execute_activity(apply_plan, args=[order_id, info["subscriber_id"], info["addon_code"]],
                                            start_to_close_timeout=timedelta(seconds=30), retry_policy=APPLY_RETRY)
        return await self._finish(order_id, result == "CONFIRMED",
                                  "Your new tariff is active from today. This month is pro-rated by day: old tariff until yesterday, new from today.")


WORKFLOWS = [AddonPurchaseWorkflow, PlanChangeWorkflow]
