"""order-adapter service: the ONLY way into the legacy order systems.

Temporal activities never talk to a legacy system. They call this service's TMF622 ProductOrder API;
it picks the country adapter (REST in DE, SOAP in PL), translates, and protects each legacy system with a
per-country rate limit shared by all pods (Valkey, 1-second window). Over capacity it answers 429 +
Retry-After, and the workflow's activity retry backs off: a bill-day spike becomes a durable queue in
Temporal instead of an overload of the legacy system.

Legacy confirmations come back asynchronously as events on the `bss-events` stream (published here on
behalf of the legacy system); the workflow worker consumes them and signals the waiting workflow.

The legacy systems themselves are emulated in this process (`legacy_emulator`): they confirm after
20-60 s and reject ~10%, like the network emulator stands in for mediation.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from telco_common.logging import configure_logging

from ..adapters.bss import BASE, TMF_STATE, adapter_for_id, for_country, market
from ..db import bss_orders, create_schema, get_engine
from ..settings import get_settings
from .common import build, serve

log = configure_logging("order-adapter")
router = APIRouter(tags=["tmf622"])


class ProductOrderIn(BaseModel):
    externalId: str = Field(min_length=8, max_length=40)
    category: str
    relatedParty: list[dict]
    productOrderItem: list[dict] = Field(min_length=1, max_length=1)
    place: list[dict]
    model_config = {"extra": "allow"}


_clock = time.time   # the rate-limit window's clock (tests pin it)


def _admit(country: str) -> bool:
    """Per-country legacy capacity, shared by every pod (fixed 1-second window in Valkey)."""
    from ..deps import get_valkey

    r = get_valkey()
    key = f"bss:rl:{country}:{int(_clock())}"
    n = r.incr(key)
    if n == 1:
        r.expire(key, 2)
    return n <= get_settings().bss_max_rps


@router.post(BASE, status_code=201)
def create(order: ProductOrderIn, response: Response) -> dict:
    o = order.model_dump()
    try:
        country = market(o)
        adapter = for_country(country)
    except (StopIteration, LookupError) as e:
        raise HTTPException(422, f"unsupported market: {e}") from e
    if not _admit(country):
        response.headers["Retry-After"] = "1"
        raise HTTPException(429, f"legacy order system for {country} is at capacity", headers={"Retry-After": "1"})
    bid = adapter.submit(get_engine(), o)
    log.info("tmf622_order_submitted", market=country, protocol=adapter.protocol, id=bid)
    return {"id": bid, "externalId": o["externalId"], "state": "acknowledged", "@type": "ProductOrder"}


@router.get(BASE + "/{order_id}")
def read(order_id: str) -> dict:
    a = adapter_for_id(order_id)
    if a is None:
        with get_engine().connect() as cx:
            legacy = cx.execute(select(bss_orders.c.status).where(bss_orders.c.id == order_id)).scalar()
        if legacy is None:
            raise HTTPException(404, "order not found")
        return {"id": order_id, "state": TMF_STATE.get(legacy, "pending")}
    state = a.state(get_engine(), order_id)
    if state is None:
        raise HTTPException(404, "order not found")
    return {"id": order_id, "state": state}


async def legacy_emulator(stop: asyncio.Event) -> None:
    """Emulated legacy systems: process SUBMITTED orders after LEGACY_MIN_S..LEGACY_MAX_S (default 2-5 s) and
    publish the outcome; LEGACY_REJECT_RATE (default 0) rejects that share of orders, to exercise the
    compensation path on demand. (It was 20-60 s with 10% random rejections: a customer approving an add-on
    waited ~40 s, and one in ten approvals failed for no visible reason.)"""
    import os

    lo, hi = float(os.environ.get("LEGACY_MIN_S", "2")), float(os.environ.get("LEGACY_MAX_S", "5"))
    reject_rate = float(os.environ.get("LEGACY_REJECT_RATE", "0"))
    from confluent_kafka import Producer
    from telco_common.streaming import StreamSettings

    engine = get_engine()
    producer = Producer(StreamSettings().client_config() | {"acks": "all"})
    while not stop.is_set():
        try:
            with engine.connect() as cx:
                due = cx.execute(select(bss_orders).where(bss_orders.c.status == "SUBMITTED")).all()
            now = datetime.now(UTC)
            for b in due:
                created = b.created_at if b.created_at.tzinfo else b.created_at.replace(tzinfo=UTC)
                if now - created < timedelta(seconds=random.uniform(lo, hi)):
                    continue
                result = "REJECTED" if random.random() < reject_rate else "CONFIRMED"
                reason = None if result == "CONFIRMED" else "offer not eligible"
                with engine.begin() as cx:
                    cx.execute(update(bss_orders).where(bss_orders.c.id == b.id, bss_orders.c.status == "SUBMITTED")
                               .values(status=result, processed_at=now, reason=reason))
                producer.produce("bss-events", key=b.order_id, value=json.dumps({
                    "type": "BSS_ORDER_RESULT", "bss_order_id": b.id, "order_id": b.order_id,
                    "state": TMF_STATE[result], "result": result, "reason": reason, "at": now.isoformat()}).encode())
                log.info("legacy_processed", id=b.id, result=result)
            producer.flush(10)
        except Exception as e:  # noqa: BLE001 - keep emulating; next tick retries
            log.warning("legacy_emulator_error", error=str(e)[:200])
        await asyncio.sleep(1)


@asynccontextmanager
async def lifespan(_):
    create_schema(get_engine())
    stop = asyncio.Event()
    task = asyncio.create_task(legacy_emulator(stop))
    yield
    stop.set()
    task.cancel()


app = build("order-adapter", [router], lifespan)


def run() -> None:
    serve(app)
