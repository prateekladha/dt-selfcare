"""usage-collector: the network-facing ingestion API (mediation -> platform).

POST /v1/usage-batches is idempotent per batch_id:
  new batch       -> 202, published to the stream
  already done    -> 200 {"status": "duplicate"} (network re-sent the same file)
  in flight       -> 409 (concurrent duplicate; caller retries)
  stream down     -> 503 + Retry-After (caller retries; partial publishes dedupe by msg_id)
Sources authenticate with a per-source token (mounted from OCI Vault).
"""
from __future__ import annotations

import hmac
import json
import os
import threading
from contextlib import asynccontextmanager

import valkey
from confluent_kafka import Producer
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.concurrency import run_in_threadpool
from prometheus_client import Counter, Histogram, make_asgi_app
from pydantic_settings import BaseSettings, SettingsConfigDict
from telco_common.logging import configure_logging
from telco_common.models import UsageBatch
from telco_common.streaming import StreamSettings

from .publisher import build_messages, publish

BATCHES = Counter("collector_batches_total", "Batches received", ["result"])
RECORDS = Counter("collector_records_total", "Records accepted")
PUBLISH_SECONDS = Histogram("collector_publish_seconds", "Time to get broker acks for a batch")

log = configure_logging("usage-collector")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")
    valkey_url: str = "redis://valkey:6379/0"
    # JSON map source -> token, e.g. {"pgw-fra-01": "…"}; mounted as a secret.
    source_tokens: str = "{}"
    batch_done_ttl_s: int = 7 * 24 * 3600


class State:
    settings: Settings
    stream: StreamSettings
    r: valkey.Valkey
    producer: Producer
    tokens: dict[str, str]
    stop: threading.Event


S = State()


def _poll_loop() -> None:
    while not S.stop.is_set():
        S.producer.poll(0.1)


@asynccontextmanager
async def lifespan(_: FastAPI):
    S.settings, S.stream = Settings(), StreamSettings()
    S.tokens = json.loads(S.settings.source_tokens)
    S.r = valkey.Valkey.from_url(S.settings.valkey_url, socket_timeout=3)
    S.producer = Producer(S.stream.client_config() | {
        "acks": "all", "linger.ms": 10, "compression.type": "none",  # payload already gzip
        "queue.buffering.max.kbytes": 256_000, "message.max.bytes": 1_000_000, "retries": 5,
    })
    S.stop = threading.Event()
    t = threading.Thread(target=_poll_loop, daemon=True)
    t.start()
    log.info("collector_started", topic=S.stream.usage_topic, partitions=S.stream.partitions)
    yield
    S.stop.set()
    S.producer.flush(10)


app = FastAPI(title="usage-collector", version="0.1.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


def authenticate(authorization: str = Header(default=""), x_source: str = Header(default="")) -> str:
    expected = S.tokens.get(x_source)
    token = authorization.removeprefix("Bearer ").strip()
    if not expected or not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=401, detail="unknown source or bad token")
    return x_source


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    try:
        S.r.ping()
    except valkey.ValkeyError as e:
        raise HTTPException(503, f"valkey: {e}") from e
    return {"status": "ready"}


@app.post("/v1/usage-batches", status_code=202)
async def ingest(batch: UsageBatch, response: Response, source: str = Depends(authenticate)) -> dict:
    if batch.source != source:
        raise HTTPException(403, "batch.source does not match authenticated source")
    key = f"batch:{batch.batch_id}"
    status = await run_in_threadpool(S.r.get, key)
    if status == b"done":
        BATCHES.labels("duplicate").inc()
        response.status_code = 200
        return {"status": "duplicate", "batch_id": batch.batch_id}
    if not await run_in_threadpool(lambda: S.r.set(key, "inflight", nx=True, ex=120)):
        BATCHES.labels("inflight").inc()
        raise HTTPException(409, "batch is being processed; retry later")

    try:
        msgs = build_messages(batch, S.stream.partitions)
        with PUBLISH_SECONDS.time():
            errors = await run_in_threadpool(publish, S.producer, S.stream.usage_topic, batch.batch_id, msgs)
    except BufferError:
        errors = ["producer queue full"]
    if errors:
        await run_in_threadpool(S.r.delete, key)
        BATCHES.labels("failed").inc()
        log.warning("publish_failed", batch_id=batch.batch_id, errors=errors[:3])
        raise HTTPException(503, "stream unavailable, retry", headers={"Retry-After": "5"})

    await run_in_threadpool(lambda: S.r.set(key, "done", ex=S.settings.batch_done_ttl_s))
    BATCHES.labels("accepted").inc()
    RECORDS.inc(len(batch.records))
    return {"status": "accepted", "batch_id": batch.batch_id, "records": len(batch.records), "messages": len(msgs)}


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_config=None)
