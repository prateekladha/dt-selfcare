"""workflow-worker deployable: Temporal worker for the order workflows + the `bss-events` consumer.

Legacy results arrive as events on `bss-events` (published by the order-adapter); this process turns
them into workflow signals. Workflows also poll the adapter, so a lost event never strands an order.
The worker never talks to a legacy system.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading

from telco_common.logging import configure_logging
from temporalio.client import Client
from temporalio.worker import Worker

from ..db import create_schema, get_engine
from ..settings import get_settings
from .addon import ACTIVITIES, TASK_QUEUE, WORKFLOWS

log = configure_logging("workflow-worker")


def bss_events_consumer(client: Client, loop: asyncio.AbstractEventLoop, stop: threading.Event) -> None:
    """`bss-events` (legacy results published by the order-adapter) -> signal the waiting workflow.
    Runs in a thread (Kafka client is blocking); signals are scheduled on the worker's event loop."""
    from confluent_kafka import Consumer
    from telco_common.streaming import StreamSettings

    c = Consumer(StreamSettings().client_config() | {"group.id": "order-workflows", "enable.auto.commit": False,
                                                     "auto.offset.reset": "earliest"})
    c.subscribe(["bss-events"])
    while not stop.is_set():
        for m in c.consume(num_messages=50, timeout=1.0):
            if m.error():
                continue
            try:
                ev = json.loads(m.value())
                fut = asyncio.run_coroutine_threadsafe(
                    client.get_workflow_handle(ev["order_id"]).signal("bss_callback", ev["result"]), loop)
                fut.result(timeout=10)
                log.info("bss_event_signalled", order=ev["order_id"][-6:], result=ev["result"])
            except Exception as e:  # noqa: BLE001 - workflow finished/unknown: its polling covers anything missed
                log.warning("bss_signal_failed", error=str(e)[:160])
        c.commit(asynchronous=True)
    c.close()


async def amain() -> None:
    create_schema(get_engine())
    for attempt in range(60):  # server may still be starting (rollout order): wait instead of crash-looping
        try:
            client = await Client.connect(os.environ.get("TEMPORAL_ADDRESS", "temporal:7233"),
                                          namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"))
            break
        except RuntimeError:
            log.warning("temporal_not_ready", attempt=attempt)
            await asyncio.sleep(min(2 + attempt, 10))
    else:
        raise SystemExit("temporal unreachable")
    stop = threading.Event()
    # Legacy BSS can take only a few hundred calls/s: Temporal enforces a task-queue-wide activity rate
    # (across ALL worker pods) and a per-pod concurrency cap. Excess work waits durably in the queue
    # instead of hammering the legacy system; a bill-day spike becomes a backlog, not an outage.
    s = get_settings()
    worker = Worker(client, task_queue=TASK_QUEUE, workflows=WORKFLOWS, activities=ACTIVITIES,
                    max_task_queue_activities_per_second=s.bss_max_rps,
                    max_concurrent_activities=s.bss_max_concurrency)
    log.info("workflow_worker_started", task_queue=TASK_QUEUE)
    threading.Thread(target=bss_events_consumer, args=(client, asyncio.get_running_loop(), stop), daemon=True).start()
    try:
        await worker.run()
    finally:
        stop.set()


def main() -> None:
    asyncio.run(amain())
