"""Subscriber usage state in Valkey + the atomic apply step.

Key layout (all hash-tagged by stream partition so one message = one atomic script):
  off:{p}                     last applied stream offset of partition p
  seen:{p}:<msg_id>           message ids applied recently (TTL)
  alerts:{p}                  outbox stream of threshold alerts (relayed to the alerts topic)
  u:{p}:<subscriber>:<cycle>  usage counters for one billing cycle + alert flags
  prof:{p}:<subscriber>       profile synced from CRM/core-api: plan_code, allowance_bytes, country
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from importlib.resources import files

import valkey

from telco_common.catalog import MB, get_addon, get_plan
from telco_common.models import EventKind, UsageRecord
from telco_common.partitioning import partition_for, slot_tag

LUA = files("telco_common").joinpath("apply.lua").read_text()

SEEN_TTL_S = 7 * 24 * 3600
USAGE_TTL_S = 400 * 24 * 3600  # > 12 months of history for trends / disputes


def usage_key(partition: int, subscriber_id: str, cycle: str) -> str:
    return f"u:{slot_tag(partition)}:{subscriber_id}:{cycle}"


def profile_key(partition: int, subscriber_id: str) -> str:
    return f"prof:{slot_tag(partition)}:{subscriber_id}"


def offset_key(partition: int) -> str:
    return f"off:{slot_tag(partition)}"


def outbox_key(partition: int) -> str:
    return f"alerts:{slot_tag(partition)}"


@dataclass(frozen=True)
class ApplyResult:
    applied: bool
    alerts: int
    reason: str


class UsageState:
    def __init__(self, client: valkey.Valkey, partitions: int):
        self.r = client
        self.partitions = partitions
        self._script = client.register_script(LUA)

    # ---- profile (written by core-api when a subscriber is provisioned or changes plan) ----
    def upsert_profile(self, subscriber_id: str, plan_code: str) -> None:
        plan = get_plan(plan_code)
        p = partition_for(subscriber_id, self.partitions)
        self.r.hset(profile_key(p, subscriber_id), mapping={
            "plan_code": plan.code, "country": plan.country, "allowance_bytes": plan.data_allowance_bytes,
        })

    def profile(self, subscriber_id: str) -> dict[str, str]:
        p = partition_for(subscriber_id, self.partitions)
        return {k.decode(): v.decode() for k, v in self.r.hgetall(profile_key(p, subscriber_id)).items()}

    def _raw(self, subscriber_id: str, cycle: str) -> dict[str, int]:
        p = partition_for(subscriber_id, self.partitions)
        return {k.decode(): int(v) for k, v in self.r.hgetall(usage_key(p, subscriber_id, cycle)).items()
                if not k.startswith(b"alert:")}

    def counters(self, subscriber_id: str, cycle: str) -> dict[str, int]:
        """Cycle totals (the per-day fields `<key>@DD` are excluded)."""
        return {k: v for k, v in self._raw(subscriber_id, cycle).items() if "@" not in k}

    def daily(self, subscriber_id: str, cycle: str) -> dict[int, dict[str, int]]:
        """{day_of_month: {counter_key: quantity}} from the per-day fields written in the same atomic apply."""
        out: dict[int, dict[str, int]] = defaultdict(dict)
        for k, v in self._raw(subscriber_id, cycle).items():
            if "@" in k:
                key, day = k.rsplit("@", 1)
                out[int(day)][key] = v
        return dict(out)

    def split_by_days(self, subscriber_id: str, cycle: str, ranges: list[tuple[int, int]]) -> list[dict[str, int]] | None:
        """Counters per inclusive day range (tariff segments). Usage recorded before per-day fields existed
        (totals without day detail) goes to the last range. None if there is no day detail at all."""
        raw = self._raw(subscriber_id, cycle)
        totals = {k: v for k, v in raw.items() if "@" not in k}
        days = self.daily(subscriber_id, cycle)
        if not days and totals:
            return None
        out: list[dict[str, int]] = [defaultdict(int) for _ in ranges]
        for i, (a, b) in enumerate(ranges):
            for d in range(a, b + 1):
                for k, v in days.get(d, {}).items():
                    out[i][k] += v
        for k, total in totals.items():
            if k == "addon_bytes":
                continue  # derived from addon:<code> counts by the rating engine
            rest = total - sum(seg.get(k, 0) for seg in out)
            if rest > 0:
                out[-1][k] += rest
        return [dict(x) for x in out]

    def committed_offset(self, partition: int) -> int | None:
        v = self.r.get(offset_key(partition))
        return int(v) if v is not None else None

    def cdr_type_stats(self) -> dict[str, int]:
        """CDRs applied per usage type across all partitions (sum of per-partition counters)."""
        out: dict[str, int] = defaultdict(int)
        for p in range(self.partitions):
            for k, v in self.r.hgetall(f"stats:{slot_tag(p)}").items():
                out[k.decode()] += int(v)
        return dict(out)

    def committed_offset_for(self, subscriber_id: str) -> int | None:
        """Monotonic version of a subscriber's usage view (offset of their partition)."""
        return self.committed_offset(partition_for(subscriber_id, self.partitions))

    # ---- the hot path ----
    def apply(self, partition: int, offset: int, msg_id: str, records: list[UsageRecord],
              levels: list[int]) -> ApplyResult:
        groups: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        stats: dict[str, int] = defaultdict(int)
        for rec in records:
            stats[str(rec.usage_type or rec.kind)] += 1
            if partition_for(rec.subscriber_id, self.partitions) != partition:
                raise ValueError(f"record for {rec.subscriber_id} does not belong to partition {partition}")
            fields = groups[(rec.subscriber_id, rec.cycle, rec.country)]
            day = f"@{rec.event_time.day:02d}"  # day of the EVENT (UTC): a late CDR lands on the day it happened
            if rec.kind == EventKind.PLAN_CHANGED:
                addon = get_addon(rec.addon_code or "")
                fields[f"addon:{addon.code}"] += 1
                fields[f"addon:{addon.code}{day}"] += 1
                fields["addon_bytes"] += addon.data_mb * MB
            else:
                key = f"{rec.usage_type}:{rec.roaming_zone}" if rec.roaming_zone else str(rec.usage_type)
                fields[key] += rec.quantity      # cycle total: alerts, live unbilled, bill run
                fields[key + day] += rec.quantity  # same increment per day: trend chart + exact pro-rating

        keys = [offset_key(partition), f"seen:{slot_tag(partition)}:{msg_id}", outbox_key(partition),
                f"stats:{slot_tag(partition)}"]
        args: list[str | int] = [offset, msg_id, SEEN_TTL_S, USAGE_TTL_S, ",".join(map(str, sorted(levels))),
                                 len(groups)]
        for (sub, cycle, country), fields in groups.items():
            keys += [usage_key(partition, sub, cycle), profile_key(partition, sub)]
            args += [sub, cycle, country, len(fields)]
            for f, inc in fields.items():
                args += [f, inc]
        args.append(len(stats))
        for f, inc in stats.items():
            args += [f, inc]
        applied, alerts, reason = self._script(keys=keys, args=args)
        return ApplyResult(bool(applied), int(alerts), reason.decode() if isinstance(reason, bytes) else reason)
