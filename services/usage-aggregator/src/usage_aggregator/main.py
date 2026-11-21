"""usage-aggregator: consumes usage-events, maintains per-subscriber totals, raises alerts.

Offsets live in Valkey next to the state (see apply.lua); the broker's consumer-group offsets
are committed too but only for lag monitoring. On partition assignment we seek to the offset
stored in Valkey, so a crash/rebalance resumes exactly after the last *applied* message.
"""
from __future__ import annotations

import time

import valkey
from confluent_kafka import OFFSET_BEGINNING, Consumer, KafkaException
from prometheus_client import Counter, Gauge, Histogram
from telco_common.codec import decode_records
from telco_common.logging import configure_logging
from telco_common.streaming import StreamSettings
from telco_common.usage_state import UsageState

from .runtime import AlertLevels, Settings, bootstrap

MSGS = Counter("aggregator_messages_total", "Stream messages handled", ["result"])
RECORDS = Counter("aggregator_records_total", "Usage records applied")
ALERTS = Counter("aggregator_alerts_raised_total", "Threshold alerts written to the outbox")
APPLY_SECONDS = Histogram("aggregator_apply_seconds", "Valkey apply latency per message")
EVENT_LAG = Histogram("aggregator_event_lag_seconds", "event_time -> applied, oldest record per message",
                      buckets=(1, 5, 15, 30, 60, 120, 300, 900, 3600))
ASSIGNED = Gauge("aggregator_assigned_partitions", "Partitions owned by this pod")

log = configure_logging("usage-aggregator")


def run() -> None:
    settings, stream = Settings(), StreamSettings()
    shutdown = bootstrap(settings)
    state = UsageState(valkey.Valkey.from_url(settings.valkey_url, socket_timeout=5), stream.partitions)
    levels = AlertLevels()

    consumer = Consumer(stream.client_config() | {
        "group.id": settings.consumer_group,
        "enable.auto.commit": False,
        "auto.offset.reset": "earliest",
        "session.timeout.ms": 30_000,
        "max.poll.interval.ms": 300_000,
    })

    def on_assign(c: Consumer, parts: list) -> None:
        for tp in parts:
            stored = state.committed_offset(tp.partition)
            tp.offset = stored + 1 if stored is not None else OFFSET_BEGINNING
        c.assign(parts)
        ASSIGNED.set(len(parts))
        log.info("partitions_assigned", partitions=[(tp.partition, tp.offset) for tp in parts])

    def on_revoke(c: Consumer, parts: list) -> None:
        ASSIGNED.set(0)
        log.info("partitions_revoked", partitions=[tp.partition for tp in parts])

    consumer.subscribe([stream.usage_topic], on_assign=on_assign, on_revoke=on_revoke)
    log.info("aggregator_started", topic=stream.usage_topic, partitions=stream.partitions)
    try:
        while not shutdown.requested:
            msgs = consumer.consume(num_messages=settings.batch_size, timeout=1.0)
            for m in msgs:
                if m.error():
                    log.warning("consume_error", error=str(m.error()))
                    continue
                handle(state, levels, m)
            if msgs:
                consumer.commit(asynchronous=True)  # lag visibility only; truth is in Valkey
    finally:
        consumer.close()
        log.info("aggregator_stopped")


def handle(state: UsageState, levels: AlertLevels, m) -> None:
    headers = dict(m.headers() or [])
    msg_id = (headers.get("msg_id") or f"{m.partition()}-{m.offset()}".encode()).decode()
    try:
        records = decode_records(m.value())
    except Exception as e:  # noqa: BLE001 - poison message must not block the partition
        MSGS.labels("poison").inc()
        state.r.lpush(f"dlq:{{p{m.partition()}}}", m.value())
        state.r.ltrim(f"dlq:{{p{m.partition()}}}", 0, 9999)
        log.error("poison_message", partition=m.partition(), offset=m.offset(), error=str(e))
        return

    backoff = 0.2
    while True:  # Valkey unavailable -> block (backpressure); never skip or double-apply
        try:
            with APPLY_SECONDS.time():
                res = state.apply(m.partition(), m.offset(), msg_id, records, levels.get())
            break
        except (valkey.ConnectionError, valkey.TimeoutError) as e:
            log.warning("valkey_unavailable_retrying", error=str(e), backoff=backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 10)

    MSGS.labels(res.reason).inc()
    if res.applied:
        RECORDS.inc(len(records))
        ALERTS.inc(res.alerts)
        oldest = min(r.event_time for r in records).timestamp()
        EVENT_LAG.observe(max(0.0, time.time() - oldest))


def main() -> None:
    try:
        run()
    except KafkaException as e:
        log.error("kafka_fatal", error=str(e))
        raise


if __name__ == "__main__":
    main()
