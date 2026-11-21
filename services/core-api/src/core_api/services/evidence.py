"""Evidence trail: prove a bill's numbers come from CDRs.

Re-reads the subscriber's raw CDRs from the Object Storage archive (written by raw-archiver from
the stream, independently of the real-time aggregator), re-sums them per usage key, and reconciles
the result against (a) the Valkey running totals and (b) the issued bill. Any mismatch is shown.

Demo-scale implementation: scans the archive objects of the subscriber's stream partition (the
archive is laid out raw/<topic>/dt=<day>/p=<partition>/<offsets>.jsonl.gz). In production this
query runs on the lakehouse (e.g. ADW external tables over the same objects) by subscriber + cycle.
"""
from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import lru_cache

from telco_common.catalog import MB, get_addon
from telco_common.codec import iter_json_lines
from telco_common.models import cycle_of
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState

BUCKET = os.getenv("APP_ARCHIVE_BUCKET", "dt-selfcare-usage-raw")
TOPIC = os.getenv("STREAM_USAGE_TOPIC", "usage-events")
UNITS = {"DATA": "bytes", "ROAMING_DATA": "bytes", "VOICE": "seconds", "ROAMING_VOICE": "seconds",
         "INTL_VOICE": "seconds", "SMS": "count", "addon": "count", "addon_bytes": "bytes"}


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


def _scan(subscriber_id: str, cycle: str, partitions: int) -> dict:
    client, ns = _client()
    part = partition_for(subscriber_id, partitions)
    names, start = [], None
    while True:
        resp = client.list_objects(ns, BUCKET, prefix=f"raw/{TOPIC}/", start=start, fields="name,size")
        names += [o.name for o in resp.data.objects if f"/p={part}/" in o.name]
        start = resp.data.next_start_with
        if not start:
            break

    def read(name: str) -> list[dict]:
        body = client.get_object(ns, BUCKET, name).data.content
        text = gzip.decompress(body).decode()
        if subscriber_id not in text:  # cheap pre-filter before JSON parsing
            return []
        return [r | {"_object": name} for r in iter_json_lines(text)
                if r["subscriber_id"] == subscriber_id and cycle_of(datetime.fromisoformat(r["event_time"])) == cycle]

    seen: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for recs in pool.map(read, names):
            for r in recs:
                seen.setdefault(r["record_id"], r)  # archive is at-least-once: dedupe by record_id
    return {"partition": part, "objects_scanned": len(names), "records": list(seen.values())}


def cdr_evidence(state: UsageState, subscriber_id: str, cycle: str, sample_per_key: int = 4) -> dict:
    cache_key = f"ev:{subscriber_id}:{cycle}"
    cached = state.r.get(cache_key)
    if cached:
        return json.loads(cached)

    scan = _scan(subscriber_id, cycle, state.partitions)
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
    recon = [{"key": k, "unit": UNITS.get(k.split(":")[0], ""), "cdr_records": counts.get(k, 0),
              "from_cdrs": sums.get(k, 0), "valkey_counter": int(counters.get(k, 0)),
              "match": sums.get(k, 0) == int(counters.get(k, 0))} for k in keys]
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
        "reconciliation": recon, "all_match": all(x["match"] for x in recon), "sample": sample,
    }
    is_current = cycle == cycle_of(datetime.now().astimezone())
    state.r.set(cache_key, json.dumps(out), ex=60 if is_current else 900)
    return out
