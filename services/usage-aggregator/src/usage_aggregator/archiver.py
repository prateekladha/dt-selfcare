"""raw-archiver: copies every usage message to Object Storage for audit, disputes, reconciliation.

Independent consumer group, so the archive never slows down the real-time path. Messages are
already gzip; concatenated gzip members form a valid gzip file, so we just append bytes.
Objects are named by offset range, which makes the layout easy to replay or reprocess.
Delivery is at-least-once (a crash before commit can re-upload an overlapping range); the
reconciliation job dedupes by record_id, which is cheap in batch.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from confluent_kafka import Consumer
from prometheus_client import Counter
from telco_common.logging import configure_logging
from telco_common.streaming import StreamSettings

from .runtime import Settings, bootstrap, oci_object_storage

UPLOADED = Counter("archiver_objects_uploaded_total", "Raw archive objects written")
BYTES = Counter("archiver_bytes_total", "Compressed bytes archived")

log = configure_logging("raw-archiver")


class PartitionBuffer:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.size = 0
        self.first: int | None = None
        self.last = -1
        self.started = time.monotonic()

    def add(self, offset: int, payload: bytes) -> None:
        if self.first is None:
            self.first, self.started = offset, time.monotonic()
        self.chunks.append(payload)
        self.size += len(payload)
        self.last = offset


def run() -> None:
    settings, stream = Settings(), StreamSettings()
    shutdown = bootstrap(settings)
    os_client = oci_object_storage(settings)
    namespace = settings.archive_namespace or os_client.get_namespace().data
    consumer = Consumer(stream.client_config() | {
        "group.id": "raw-archiver", "enable.auto.commit": False, "auto.offset.reset": "earliest",
    })
    consumer.subscribe([stream.usage_topic])
    buffers: dict[int, PartitionBuffer] = {}

    def flush(partition: int) -> None:
        buf = buffers.pop(partition, None)
        if not buf or buf.first is None:
            return
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        name = f"raw/{stream.usage_topic}/dt={day}/p={partition}/{buf.first:012d}-{buf.last:012d}.jsonl.gz"
        os_client.put_object(namespace, settings.archive_bucket, name, b"".join(buf.chunks))
        from confluent_kafka import TopicPartition

        consumer.commit(offsets=[TopicPartition(stream.usage_topic, partition, buf.last + 1)], asynchronous=False)
        UPLOADED.inc()
        BYTES.inc(buf.size)
        log.info("archived", object=name, bytes=buf.size)

    try:
        while not shutdown.requested:
            for m in consumer.consume(num_messages=500, timeout=1.0):
                if m.error():
                    continue
                buffers.setdefault(m.partition(), PartitionBuffer()).add(m.offset(), m.value())
            for p, buf in list(buffers.items()):
                if buf.size >= settings.archive_flush_bytes or \
                        time.monotonic() - buf.started >= settings.archive_flush_seconds:
                    flush(p)
        for p in list(buffers):
            flush(p)
    finally:
        consumer.close()


def main() -> None:
    run()
