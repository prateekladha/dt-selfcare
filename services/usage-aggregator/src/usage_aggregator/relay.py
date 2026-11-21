"""alert-relay: moves alerts from the Valkey outbox streams to the `usage-alerts` topic.

Transactional-outbox pattern: the alert was written atomically with the state change; the relay
publishes it and XACKs only after the broker confirmed delivery. A crash means re-publish, and
downstream consumers dedupe on `alert_id` (subscriber:cycle:level), so users get one alert.
"""
from __future__ import annotations

import json

import valkey
from confluent_kafka import Producer
from prometheus_client import Counter
from telco_common.logging import configure_logging
from telco_common.streaming import StreamSettings

from .runtime import Settings, bootstrap
from .state import outbox_key

RELAYED = Counter("relay_alerts_published_total", "Alerts published to the alerts topic")
GROUP, CONSUMER = "relay", "relay-0"

log = configure_logging("alert-relay")


def run() -> None:
    settings, stream = Settings(), StreamSettings()
    shutdown = bootstrap(settings)
    r = valkey.Valkey.from_url(settings.valkey_url, socket_timeout=10)
    producer = Producer(stream.client_config() | {"acks": "all", "linger.ms": 20, "enable.idempotence": False})
    streams = [outbox_key(p) for p in range(stream.partitions)]
    for s in streams:
        try:
            r.xgroup_create(s, GROUP, id="0", mkstream=True)
        except valkey.ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    def publish(stream_key: str, entry_id: bytes, fields: dict[bytes, bytes]) -> None:
        alert = {k.decode(): v.decode() for k, v in fields.items()}

        def delivered(err, _msg) -> None:
            if err is None:
                r.xack(stream_key, GROUP, entry_id)
                RELAYED.inc()
            else:
                log.warning("publish_failed_will_retry", alert_id=alert["alert_id"], error=str(err))

        producer.produce(stream.alerts_topic, key=alert["subscriber_id"], value=json.dumps(alert).encode(),
                         headers={"alert_id": alert["alert_id"]}, on_delivery=delivered)

    log.info("relay_started", streams=len(streams))
    while not shutdown.requested:
        # 1) re-drive entries that were read but never acked (crash / failed publish)
        for s in streams:
            _, claimed, _ = r.xautoclaim(s, GROUP, CONSUMER, min_idle_time=30_000, start_id="0-0", count=100)
            for entry_id, fields in claimed:
                publish(s, entry_id, fields)
        # 2) new entries
        resp = r.xreadgroup(GROUP, CONSUMER, {s: ">" for s in streams}, count=200, block=1000) or []
        for s, entries in resp:
            for entry_id, fields in entries:
                publish(s.decode(), entry_id, fields)
        producer.poll(0)
        producer.flush(5)
    producer.flush(10)


def main() -> None:
    run()
