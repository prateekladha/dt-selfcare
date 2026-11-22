"""One-off: rebuild missing per-day usage fields (`<counter>@DD`) from the raw CDR archive.

Why: the aggregator writes a per-day field next to every cycle total in the same atomic apply, but only
since per-day fields were introduced. Usage applied before that has totals without day detail, so the
daily chart shows less than the meter (and tariff pro-rating has to put that usage on the last segment).

How (idempotent, safe while live traffic flows):
  1. Read the subscriber's archived CDRs (the audit copy raw-archiver writes from the same stream),
     dedupe by record_id and sum them per cycle, day of the event (UTC) and counter key, exactly as
     the aggregator keys them.
  2. Per counter: gap = cycle total - sum of the existing day fields. Only the gap is filled, never more;
     totals are never touched. Days are filled in date order with what the archive has for that day
     beyond what the day field already holds.
  3. Written with WATCH/MULTI on the usage hash, re-reading the hash on conflict, so a live CDR applied
     in between can't be double counted. A second run finds gap = 0 everywhere and changes nothing.
  Anything the archive cannot explain stays without day detail and is reported (never invented).

  python -m core_api.backfill_daily            # dry run: prints what it would write
  python -m core_api.backfill_daily --apply
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime

import valkey
from telco_common.models import cycle_of
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState, usage_key

from .services.evidence import subscriber_records


def _field(rec: dict) -> str:
    """Counter key exactly as UsageState.apply builds it."""
    if rec.get("kind") == "PLAN_CHANGED":
        return f"addon:{rec['addon_code']}"
    return f"{rec['usage_type']}:{rec['roaming_zone']}" if rec.get("roaming_zone") else str(rec["usage_type"])


def archive_by_day(records: list[dict]) -> dict[str, dict[int, dict[str, int]]]:
    """{cycle: {day: {field: quantity}}} from deduped archive records."""
    out: dict[str, dict[int, dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for r in records:
        t = datetime.fromisoformat(r["event_time"])
        qty = 1 if r.get("kind") == "PLAN_CHANGED" else int(r["quantity"])
        out[cycle_of(t)][t.day][_field(r)] += qty
    return out


def plan_fill(raw: dict[str, int], archive: dict[int, dict[str, int]]) -> tuple[dict[str, int], dict[str, int]]:
    """(increments {field@DD: n}, unexplained {field: n}) for one usage hash.

    raw is the hash without alert flags; archive is {day: {field: qty}} for the same cycle."""
    totals = {k: v for k, v in raw.items() if "@" not in k and k != "addon_bytes"}
    have: dict[str, dict[int, int]] = defaultdict(dict)
    for k, v in raw.items():
        if "@" in k:
            f, d = k.rsplit("@", 1)
            have[f][int(d)] = v
    inc: dict[str, int] = {}
    left: dict[str, int] = {}
    for f, total in totals.items():
        gap = total - sum(have[f].values())
        for d in sorted(archive):
            if gap <= 0:
                break
            spare = archive[d].get(f, 0) - have[f].get(d, 0)
            if spare > 0:
                n = min(spare, gap)
                inc[f"{f}@{d:02d}"] = n
                gap -= n
        if gap > 0:
            left[f] = gap
    return inc, left


def backfill_subscriber(state: UsageState, subscriber_id: str, apply: bool) -> list[dict]:
    recs = subscriber_records(state, subscriber_id)["records"]
    report = []
    part = partition_for(subscriber_id, state.partitions)
    for cycle, days in sorted(archive_by_day(recs).items()):
        key = usage_key(part, subscriber_id, cycle)
        with state.r.pipeline() as pipe:
            while True:
                try:
                    pipe.watch(key)
                    raw = {k.decode(): int(v) for k, v in pipe.hgetall(key).items() if not k.startswith(b"alert:")}
                    inc, left = plan_fill(raw, days)
                    if apply and inc:
                        pipe.multi()
                        for f, n in inc.items():
                            pipe.hincrby(key, f, n)
                        pipe.execute()
                    else:
                        pipe.unwatch()
                    break
                except valkey.WatchError:
                    continue  # a live CDR landed in between: re-read and re-plan
        if inc or left:
            report.append({"subscriber": subscriber_id, "cycle": cycle, "fields_written": len(inc),
                           "quantity_attributed": sum(inc.values()), "unexplained": left})
    return report


def main() -> None:
    from telco_common.demo import DEMO_SUBSCRIBERS

    from .deps import get_usage_state

    apply = "--apply" in sys.argv
    state = get_usage_state()
    for s in DEMO_SUBSCRIBERS:
        for row in backfill_subscriber(state, s.subscriber_id, apply):
            print(("APPLIED " if apply else "DRY-RUN ") + str(row), flush=True)
    print("done", "(applied)" if apply else "(dry run: pass --apply to write)")


if __name__ == "__main__":
    main()
