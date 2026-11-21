"""AddonPurchaseWorkflow on Temporal's time-skipping test server: confirm, reject, lost callback (poll)."""
import asyncio
import uuid

import pytest
from core_api.workflows.addon import AddonPurchaseWorkflow
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

calls: list = []
BSS = {"status": "SUBMITTED"}


@activity.defn(name="validate_order")
async def validate_order(order_id: str) -> dict:
    calls.append("validate")
    return {"subscriber_id": "4915100000003", "addon_code": "DE-DATA-5GB"}


@activity.defn(name="submit_to_bss")
async def submit_to_bss(order_id: str) -> str:
    calls.append("submit")
    return "BSS-1"


@activity.defn(name="bss_status")
async def bss_status(bss_order_id: str) -> str:
    calls.append("poll")
    return BSS["status"]


@activity.defn(name="apply_addon")
async def apply_addon(order_id: str, sub: str, code: str) -> str:
    calls.append("apply")
    return "accepted"


@activity.defn(name="finish_order")
async def finish_order(order_id: str, status: str, detail: str) -> None:
    calls.append(f"finish:{status}")


async def run(signal: str | None, poll_status: str = "SUBMITTED") -> str:
    calls.clear()
    BSS["status"] = poll_status
    async with await WorkflowEnvironment.start_time_skipping() as env, Worker(
            env.client, task_queue="t", workflows=[AddonPurchaseWorkflow],
            activities=[validate_order, apply_addon, finish_order]), Worker(
            env.client, task_queue="legacy-bss-DE", activities=[submit_to_bss, bss_status]):
            h = await env.client.start_workflow(AddonPurchaseWorkflow.run, "o1", id=f"wf-{uuid.uuid4()}", task_queue="t")
            if signal:
                await asyncio.sleep(0.5)
                await h.signal(AddonPurchaseWorkflow.bss_callback, signal)
            return await h.result()


@pytest.mark.parametrize("signal,poll,expected,applied", [
    ("CONFIRMED", "SUBMITTED", "COMPLETED", True),      # legacy system calls back
    ("REJECTED", "SUBMITTED", "FAILED", False),         # rejected -> compensation, nothing applied
    (None, "completed", "COMPLETED", True),             # callback lost -> durable timer -> poll finds it (TMF622 state)
    (None, "acknowledged", "FAILED", False),            # never confirmed -> gives up after 30 min (time-skipped)
])
def test_addon_workflow_paths(signal, poll, expected, applied):
    assert asyncio.run(run(signal, poll)) == expected
    assert ("apply" in calls) is applied and calls[-1] == f"finish:{expected}"


def test_country_adapters_speak_tmf622_and_translate_per_market():
    from core_api.adapters.bss import TMF_STATE, for_country, product_order
    from core_api.db import create_schema
    from sqlalchemy import create_engine

    engine = create_engine("sqlite://")
    create_schema(engine)
    de, pl = for_country("DE"), for_country("PL")
    o = product_order("ord-1", "4915100000001", "DATA_5GB", "DE")
    assert o["@type"] == "ProductOrder" and o["productOrderItem"][0]["productOffering"]["id"] == "DATA_5GB"
    bid = de.submit(engine, o)
    assert bid.startswith("DEB-") and de.submit(engine, o) == bid           # idempotent
    assert '"aktion": "BUCHEN"' in de.last_wire and de.state(engine, bid) == "acknowledged"
    pid = pl.submit(engine, product_order("ord-2", "48600000001", "DATA_5GB", "PL"))
    assert pid.startswith("PLB-") and "<ord:CreateOrder>" in pl.last_wire
    assert TMF_STATE["CONFIRMED"] == "completed"
    import pytest
    with pytest.raises(LookupError):
        for_country("FR")
