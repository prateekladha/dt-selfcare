"""workflow-worker deployable: Temporal worker for order workflows + the simulated legacy BSS.

The legacy BSS emulator stands in for a country's slow order system (like the network emulator stands in
for mediation): it confirms SUBMITTED orders after 20-60 s, rejects ~10%, and calls back the workflow
with a Temporal signal. Workflows also poll, so a lost callback never strands an order.
"""
from __future__ import annotations

import asyncio
import os
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from telco_common.logging import configure_logging
from temporalio.client import Client
from temporalio.worker import Worker

from ..db import bss_orders, create_schema, get_engine
from ..settings import get_settings
from .addon import ACTIVITIES, TASK_QUEUE, AddonPurchaseWorkflow

log = configure_logging("workflow-worker")


async def legacy_bss(client: Client, stop: asyncio.Event) -> None:
    engine = get_engine()
    while not stop.is_set():
        try:
            with engine.connect() as cx:
                due = cx.execute(select(bss_orders).where(bss_orders.c.status == "SUBMITTED")).all()
            now = datetime.now(UTC)
            for b in due:
                created = b.created_at if b.created_at.tzinfo else b.created_at.replace(tzinfo=UTC)
                if now - created < timedelta(seconds=random.randint(20, 60)):
                    continue
                result = "REJECTED" if random.random() < 0.1 else "CONFIRMED"
                with engine.begin() as cx:
                    cx.execute(update(bss_orders).where(bss_orders.c.id == b.id).values(
                        status=result, processed_at=now, reason=None if result == "CONFIRMED" else "offer not eligible"))
                try:
                    await client.get_workflow_handle(b.order_id).signal(AddonPurchaseWorkflow.bss_callback, result)
                except Exception as e:  # noqa: BLE001 - the workflow's polling picks it up anyway
                    log.warning("bss_callback_failed", order=b.order_id[-6:], error=str(e)[:120])
                log.info("bss_processed", order=b.order_id[-6:], result=result)
        except Exception as e:  # noqa: BLE001
            log.warning("bss_loop_error", error=str(e)[:200])
        await asyncio.sleep(5)


async def amain() -> None:
    create_schema(get_engine())
    client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"),
                                  namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"))
    stop = asyncio.Event()
    # Legacy BSS can take only a few hundred calls/s: Temporal enforces a task-queue-wide activity rate
    # (across ALL worker pods) and a per-pod concurrency cap. Excess work waits durably in the queue
    # instead of hammering the legacy system; a bill-day spike becomes a backlog, not an outage.
    s = get_settings()
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=[AddonPurchaseWorkflow], activities=ACTIVITIES,
                    max_task_queue_activities_per_second=s.bss_max_rps,
                    max_concurrent_activities=s.bss_max_concurrency)
    log.info("workflow_worker_started", task_queue=TASK_QUEUE)
    await asyncio.gather(worker.run(), legacy_bss(client, stop))


def main() -> None:
    asyncio.run(amain())
