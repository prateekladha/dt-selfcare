"""Partition-aware, compressed, idempotent publishing of a usage batch.

One network batch -> grouped by subscriber partition -> chunks of <= CHUNK_RECORDS -> one stream
message per chunk with a deterministic msg_id. Explicit partition (not key hashing) because the
aggregator's state is hash-tagged by partition and must agree with the producer on placement.
"""
from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Protocol

from telco_common.codec import MAX_MESSAGE_BYTES, encode_records, message_id
from telco_common.models import UsageRecord
from telco_common.partitioning import partition_for

CHUNK_RECORDS = 2000  # ~2000 * 200 B = 400 KB raw -> ~80 KB gzip; far below the 1 MB message limit


@dataclass(frozen=True)
class OutMessage:
    partition: int
    msg_id: str
    payload: bytes
    records: int


def build_messages(batch_id: str, records: list[UsageRecord], partitions: int) -> list[OutMessage]:
    by_part: dict[int, list[UsageRecord]] = defaultdict(list)
    for rec in records:
        by_part[partition_for(rec.subscriber_id, partitions)].append(rec)
    out: list[OutMessage] = []
    for p, recs in sorted(by_part.items()):
        for chunk_no, start in enumerate(range(0, len(recs), CHUNK_RECORDS)):
            chunk = recs[start:start + CHUNK_RECORDS]
            payload = encode_records(chunk)
            if len(payload) > MAX_MESSAGE_BYTES:
                raise ValueError("chunk exceeds max message size; lower CHUNK_RECORDS")
            out.append(OutMessage(p, message_id(batch_id, p, chunk_no), payload, len(chunk)))
    return out


class ProducerLike(Protocol):
    def produce(self, topic: str, value: bytes, partition: int, headers: dict, on_delivery) -> None: ...
    def poll(self, timeout: float) -> int: ...


class DeliveryTracker:
    """Waits until every message of one request is acknowledged (or one fails)."""

    def __init__(self, expected: int):
        self.remaining, self.errors = expected, []
        self._lock, self.done = threading.Lock(), threading.Event()
        if expected == 0:
            self.done.set()

    def callback(self, err, _msg) -> None:
        with self._lock:
            if err is not None:
                self.errors.append(str(err))
            self.remaining -= 1
            if self.remaining == 0 or err is not None:
                self.done.set()


def publish(producer: ProducerLike, topic: str, batch_id: str, msgs: list[OutMessage],
            timeout_s: float = 10.0) -> list[str]:
    """Returns delivery errors (empty list = all acknowledged by the broker)."""
    tracker = DeliveryTracker(len(msgs))
    for m in msgs:
        producer.produce(topic, value=m.payload, partition=m.partition,
                         headers={"msg_id": m.msg_id, "batch_id": batch_id}, on_delivery=tracker.callback)
    if not tracker.done.wait(timeout_s):
        return tracker.errors or ["delivery timeout"]
    return tracker.errors
