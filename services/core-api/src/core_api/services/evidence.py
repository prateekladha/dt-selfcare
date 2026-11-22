"""Evidence trail: prove a bill's numbers come from CDRs.

Re-reads the subscriber's raw CDRs from the Object Storage archive (written by raw-archiver from
the stream, independently of the real-time aggregator), re-sums them per usage key, and reconciles
the result against (a) the Valkey running totals and (b) the issued bill. Any mismatch is shown.

Demo-scale implementation: reads the archive objects of the subscriber's stream partition (the
archive is laid out raw/<topic>/dt=<day>/p=<partition>/<offsets>.jsonl.gz), using a per-object
customer index so only the subscriber's own objects are downloaded. In production this query runs
on the lakehouse (e.g. ADW external tables over the same objects) by subscriber + cycle.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache

from sqlalchemy import select
from telco_common.catalog import MB, get_addon
from telco_common.codec import iter_json_lines
from telco_common.models import cycle_of
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState

BUCKET = os.getenv("APP_ARCHIVE_BUCKET", "dt-selfcare-usage-raw")
TOPIC = os.getenv("STREAM_USAGE_TOPIC", "usage-events")
UNITS = {"DATA": "bytes", "ROAMING_DATA": "bytes", "DATA_ZERO_RATED": "bytes", "VOICE": "seconds",
         "ROAMING_VOICE": "seconds", "ROAMING_VOICE_MT": "seconds", "VOICE_MT": "seconds", "INTL_VOICE": "seconds",
         "EMERGENCY": "seconds", "TOLLFREE": "seconds", "SMS": "count", "MMS": "count", "SMS_INTL": "count",
         "PREMIUM": "minor units", "addon": "count", "addon_bytes": "bytes"}


@lru_cache
def _client():
    import oci

    if os.getenv("APP_OCI_AUTH", "instance_principal") == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        c = oci.object_storage.ObjectStorageClient(config={}, signer=signer)
    else:
        c = oci.object_storage.ObjectStorageClient(oci.config.from_file())
    ns = os.getenv("APP_ARCHIVE_NAMESPACE") or c.get_namespace().data
    return c, ns


def _key(rec: dict) -> list[tuple[str, int]]:
    if rec.get("kind") == "PLAN_CHANGED":
        addon = get_addon(rec["addon_code"])
        return [(f"addon:{addon.code}", 1), ("addon_bytes", addon.data_mb * MB)]
    t = rec["usage_type"]
    return [((f"{t}:{rec['roaming_zone']}" if rec.get("roaming_zone") else t), int(rec["quantity"]))]


INDEX_TTL_S = 35 * 24 * 3600
_listing: dict[int, tuple[float, list[str]]] = {}
_known: tuple[float, frozenset[str]] = (0.0, frozenset())


def _partition_objects(part: int) -> list[str]:
    """Archive object names of one stream partition (cached 30 s; a list call returns 1,000 names)."""
    hit = _listing.get(part)
    if hit and time.monotonic() - hit[0] < 30:
        return hit[1]
    client, ns = _client()
    names, start = [], None
    while True:
        resp = client.list_objects(ns, BUCKET, prefix=f"raw/{TOPIC}/", start=start, fields="name")
        names += [o.name for o in resp.data.objects if f"/p={part}/" in o.name]
        start = resp.data.next_start_with
        if not start:
            break
    _listing[part] = (time.monotonic(), names)
    return names


def _customers() -> frozenset[str]:
    """Provisioned customers (the only subscribers anyone can ask evidence for). Cached 5 min."""
    global _known
    if time.monotonic() - _known[0] > 300:
        from ..db import get_engine, subscribers

        with get_engine().connect() as cx:
            _known = (time.monotonic(), frozenset(r[0] for r in cx.execute(select(subscribers.c.id))))
    return _known[1]


def subscriber_records(state: UsageState, subscriber_id: str) -> dict:
    """All archived CDRs of one subscriber (every cycle), deduped by record_id.

    Archive objects are immutable (named by offset range), so each one is read at most once to learn
    which customers it contains; that index lives in Valkey (`evidx:{p}:<customer-set hash>`, 35 days).
    After that a scan only downloads the subscriber's own objects instead of the whole partition, so
    the cost grows with the customer's own history, not with the archive (load tests, other customers).
    The index is a superset (substring match), records are then filtered exactly by subscriber_id.
    """
    client, ns = _client()
    part = partition_for(subscriber_id, state.partitions)
    names = _partition_objects(part)
    customers = _customers() | {subscriber_id}
    fp = hashlib.sha1(",".join(sorted(customers)).encode()).hexdigest()[:10]
    idx = f"evidx:{{p{part}}}:{fp}"
    known = state.r.hmget(idx, names) if names else []
    todo = [(n, v is None) for n, v in zip(names, known) if v is None or subscriber_id in v.decode().split()]

    def read(item: tuple[str, bool]) -> tuple[str, str | None, list[dict]]:
        name, unindexed = item
        text = gzip.decompress(client.get_object(ns, BUCKET, name).data.content).decode()
        present = " ".join(sorted(c for c in customers if c in text)) if unindexed else None
        if subscriber_id not in text:  # cheap pre-filter before JSON parsing
            return name, present, []
        return name, present, [r | {"_object": name} for r in iter_json_lines(text) if r["subscriber_id"] == subscriber_id]

    seen: dict[str, dict] = {}
    learned: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=16) as pool:
        for name, present, recs in pool.map(read, todo):
            if present is not None:
                learned[name] = present
            for r in recs:
                seen.setdefault(r["record_id"], r)  # archive is at-least-once: dedupe by record_id
    if learned:
        state.r.hset(idx, mapping=learned)
        state.r.expire(idx, INDEX_TTL_S)
    return {"partition": part, "objects_scanned": len(names), "objects_read": len(todo), "records": list(seen.values())}


def _scan(subscriber_id: str, cycle: str, state: UsageState) -> dict:
    out = subscriber_records(state, subscriber_id)
    out["records"] = [r for r in out["records"] if cycle_of(datetime.fromisoformat(r["event_time"])) == cycle]
    return out


def cdr_evidence(state: UsageState, subscriber_id: str, cycle: str, sample_per_key: int = 4) -> dict:
    cache_key = f"ev:{subscriber_id}:{cycle}"
    cached = state.r.get(cache_key)
    if cached:
        return json.loads(cached)
    # single flight: concurrent requests for the same evidence (several tabs/devices) wait for the first
    if not state.r.set(f"{cache_key}:lock", 1, nx=True, ex=60):
        for _ in range(90):
            time.sleep(0.5)
            if cached := state.r.get(cache_key):
                return json.loads(cached)

    try:
        out = _evidence(state, subscriber_id, cycle, sample_per_key)
    finally:
        state.r.delete(f"{cache_key}:lock")   # also on failure: the next check must not wait for a dead scan
    is_current = cycle == cycle_of(datetime.now().astimezone())
    state.r.set(cache_key, json.dumps(out), ex=60 if is_current else 900)
    return out


def _evidence(state: UsageState, subscriber_id: str, cycle: str, sample_per_key: int) -> dict:
    scan = _scan(subscriber_id, cycle, state)
    recs = scan["records"]
    sums: dict[str, int] = defaultdict(int)
    counts: dict[str, int] = defaultdict(int)
    by_key: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        for k, q in _key(r):
            sums[k] += q
            counts[k] += 1
        k0 = _key(r)[0][0]
        by_key[k0].append(r)

    counters = state.counters(subscriber_id, cycle)
    keys = sorted(set(sums) | {k for k in counters if not k.startswith("records")})
    # In the current month the archive is written about every 60 s while the live counters update at once, so
    # archive < counter is normal ("still arriving"), not a mismatch. More in the archive than counted is.
    current = cycle == cycle_of(datetime.now().astimezone())

    def state_of(k: str) -> str:
        a, c = sums.get(k, 0), int(counters.get(k, 0))
        return "match" if a == c else ("arriving" if current and a < c else "differs")
    recon = [{"key": k, "unit": UNITS.get(k.split(":")[0], ""), "cdr_records": counts.get(k, 0),
              "from_cdrs": sums.get(k, 0), "valkey_counter": int(counters.get(k, 0)),
              "state": state_of(k), "match": state_of(k) != "differs"} for k in keys]
    sample = []
    for k, rs in sorted(by_key.items()):
        for r in sorted(rs, key=lambda x: x["event_time"], reverse=True)[:sample_per_key]:
            sample.append({"record_id": r["record_id"], "event_time": r["event_time"], "key": k,
                           "quantity": r.get("quantity", 0), "unit": UNITS.get(k.split(":")[0], ""),
                           "archive_object": r["_object"].split("/", 2)[-1]})
    times = sorted(r["event_time"] for r in recs)
    out = {
        "subscriber_id": subscriber_id, "cycle": cycle, "partition": scan["partition"],
        "source": f"oci://{BUCKET}/raw/{TOPIC}/…/p={scan['partition']}/", "objects_scanned": scan["objects_scanned"],
        "cdr_count": len(recs), "first_event": times[0] if times else None, "last_event": times[-1] if times else None,
        "reconciliation": recon, "all_match": all(x["match"] for x in recon),
        "still_arriving": any(x["state"] == "arriving" for x in recon), "sample": sample,
    }
    return out
