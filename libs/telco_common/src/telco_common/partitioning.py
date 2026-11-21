"""Subscriber → partition mapping.

All events of one subscriber land on one partition (ordering + single owner of state).
OCI Streaming partition counts are fixed at stream creation, so the mapping is stable and
state keys can be hash-tagged by partition (`{p7}`), which makes a whole message's state
update a single atomic Valkey script even on a sharded cluster.
"""
import zlib


def partition_for(subscriber_id: str, partitions: int) -> int:
    if partitions <= 0:
        raise ValueError("partitions must be > 0")
    return zlib.crc32(subscriber_id.encode()) % partitions


def slot_tag(partition: int) -> str:
    return f"{{p{partition}}}"
