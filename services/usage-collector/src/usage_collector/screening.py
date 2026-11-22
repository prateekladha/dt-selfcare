"""Record-level screening at ingest (mediation-style data quality).

  malformed      -> quarantined with the validation error (batch continues)
  future-dated   -> quarantined (clock skew > 10 min)
  duplicate      -> dropped: same record_id already accepted (re-sent file under a new batch id) or
                    repeated inside the batch
Record ids are remembered for 7 days in Valkey (demo scale). At 5B records/day this becomes a
time-partitioned Bloom filter (valkey-bloom) or the stream processor's keyed state.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import ValidationError
from telco_common.catalog import ADDONS, PLANS
from telco_common.models import EventKind, UsageRecord

RECORD_TTL_S = 7 * 24 * 3600
MAX_FUTURE = timedelta(minutes=10)
# Values the rating engine knows. Anything else used to pass ingest and then fail downstream: an unknown add-on
# code crash-looped the aggregator on that partition; an unknown roaming zone broke the bill run.
KNOWN_ZONES = {"EU"} | {z for p in PLANS.values() for z in p.roaming}
MAX_QUANTITY = 10**13   # 10 TB / 10^13 s / units in one record: far beyond any real CDR, and int64-safe in totals


@dataclass
class Screened:
    valid: list[UsageRecord] = field(default_factory=list)
    quarantined: list[dict] = field(default_factory=list)
    duplicates: int = 0


def screen(raw: list[dict], r, now: datetime | None = None) -> Screened:
    now = now or datetime.now(UTC)
    out = Screened()
    parsed: list[UsageRecord] = []
    for i, rec in enumerate(raw):
        try:
            u = UsageRecord.model_validate(rec)
        except ValidationError as e:
            out.quarantined.append({"index": i, "record_id": rec.get("record_id") if isinstance(rec, dict) else None,
                                    "reason": "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:3])})
            continue
        if u.event_time > now + MAX_FUTURE:
            out.quarantined.append({"index": i, "record_id": u.record_id, "reason": "event_time is in the future"})
            continue
        reason = (f"unknown add-on code {u.addon_code!r}" if u.kind == EventKind.PLAN_CHANGED and u.addon_code not in ADDONS
                  else f"unknown roaming zone {u.roaming_zone!r}" if u.roaming_zone and u.roaming_zone not in KNOWN_ZONES
                  else "quantity is implausibly large" if u.quantity > MAX_QUANTITY else None)
        if reason:
            out.quarantined.append({"index": i, "record_id": u.record_id, "reason": reason})
            continue
        parsed.append(u)
    seen_in_batch: set[str] = set()
    unique: list[UsageRecord] = []
    for u in parsed:
        if u.record_id in seen_in_batch:
            out.duplicates += 1
            continue
        seen_in_batch.add(u.record_id)
        unique.append(u)
    if unique:
        pipe = r.pipeline(transaction=False)
        for u in unique:
            pipe.exists(f"rec:{u.record_id}")
        for u, exists in zip(unique, pipe.execute(), strict=True):
            if exists:
                out.duplicates += 1
            else:
                out.valid.append(u)
    return out


def remember(valid: list[UsageRecord], r) -> None:
    """Called only after the broker acknowledged the batch, so a failed publish can be retried."""
    pipe = r.pipeline(transaction=False)
    for u in valid:
        pipe.set(f"rec:{u.record_id}", 1, ex=RECORD_TTL_S)
    pipe.execute()


def quarantine(source: str, batch_id: str, items: list[dict], r) -> None:
    if not items:
        return
    pipe = r.pipeline(transaction=False)
    for q in items:
        pipe.lpush(f"quarantine:{source}", json.dumps(q | {"batch_id": batch_id, "at": datetime.now(UTC).isoformat()}))
    pipe.ltrim(f"quarantine:{source}", 0, 9999)
    pipe.hincrby("stats:collector", "quarantined", len(items))
    pipe.execute()
