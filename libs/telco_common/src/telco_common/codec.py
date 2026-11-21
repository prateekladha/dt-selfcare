"""Wire format for stream messages: gzip-compressed JSON lines.

Batching ~hundreds of 200-byte records per message and compressing (~5x) keeps us well under
OCI Streaming's 1 MB/s/partition write limit: 30 MB/s raw peak -> ~6 MB/s on the wire.
"""
import gzip
import hashlib
import json

from .models import UsageRecord

MAX_MESSAGE_BYTES = 900_000  # OCI Streaming max message size is 1 MB


def encode_records(records: list[UsageRecord]) -> bytes:
    # trailing newline: messages are concatenated in the raw archive, so each must end a line
    lines = "".join(r.model_dump_json(exclude_defaults=False) + "\n" for r in records)
    return gzip.compress(lines.encode(), compresslevel=6)


def iter_json_lines(text: str):
    """Robust JSONL reader: also handles archives written before the trailing-newline fix,
    where two records can share a line ('}{')."""
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \r\n\t":
            i += 1
        if i >= n:
            break
        obj, i = dec.raw_decode(text, i)
        yield obj


def decode_records(payload: bytes) -> list[UsageRecord]:
    return [UsageRecord.model_validate(o) for o in iter_json_lines(gzip.decompress(payload).decode())]


def message_id(batch_id: str, partition: int, chunk: int) -> str:
    """Deterministic id: a re-published chunk of the same batch gets the same id,
    so the aggregator can drop it even though it sits at a new offset."""
    return hashlib.sha256(f"{batch_id}|{partition}|{chunk}".encode()).hexdigest()[:32]
