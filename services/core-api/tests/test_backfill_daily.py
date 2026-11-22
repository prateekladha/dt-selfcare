"""Per-day backfill from the archive: fills only the gap, never touches totals, is idempotent."""
from datetime import UTC, datetime

import fakeredis
import pytest
from core_api import backfill_daily as bf
from telco_common.catalog import MB
from telco_common.models import Country, UsageRecord, UsageType
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState, usage_key

SUB = "4915100000001"


def rec(i: int, day: int, q: int, t=UsageType.DATA, zone=None) -> UsageRecord:
    return UsageRecord(record_id=f"rec-{i:06d}", subscriber_id=SUB, country=Country.DE, usage_type=t, roaming_zone=zone,
                       quantity=q, event_time=datetime(2026, 11, day, 10, tzinfo=UTC))


@pytest.fixture
def world(monkeypatch):
    state = UsageState(fakeredis.FakeRedis(), 4)
    p = partition_for(SUB, 4)
    old = [rec(1, 3, 100 * MB), rec(2, 3, 50 * MB), rec(3, 9, 70 * MB), rec(4, 9, 120, UsageType.VOICE),
           rec(5, 12, 30 * MB, UsageType.ROAMING_DATA, "Z2")]
    state.apply(p, 0, "m0", old, [50, 80, 100])
    key = usage_key(p, SUB, "2026-11")
    state.r.hdel(key, *[k for k in state.r.hkeys(key) if b"@" in k])  # applied before per-day fields existed
    archive = [r.model_dump(mode="json") for r in old]
    monkeypatch.setattr(bf, "subscriber_records", lambda s, sub: {"records": archive})
    return state, p, key, archive


def test_fills_days_from_archive_and_keeps_totals(world):
    state = world[0]
    totals = state.counters(SUB, "2026-11")
    bf.backfill_subscriber(state, SUB, apply=True)
    assert state.counters(SUB, "2026-11") == totals
    d = state.daily(SUB, "2026-11")
    assert d[3]["DATA"] == 150 * MB and d[9] == {"DATA": 70 * MB, "VOICE": 120} and d[12] == {"ROAMING_DATA:Z2": 30 * MB}


def test_second_run_changes_nothing(world):
    state, *_ = world
    bf.backfill_subscriber(state, SUB, apply=True)
    before = state.r.hgetall(world[2])
    assert bf.backfill_subscriber(state, SUB, apply=True) == []
    assert state.r.hgetall(world[2]) == before


def test_live_usage_with_day_fields_is_not_double_counted(world):
    state, p, _, archive = world
    live = rec(9, 22, 40 * MB)
    state.apply(p, 1, "m1", [live], [50, 80, 100])  # new pipeline writes total + @22
    archive.append(live.model_dump(mode="json"))     # ... and the archiver copied it too
    bf.backfill_subscriber(state, SUB, apply=True)
    d = state.daily(SUB, "2026-11")
    assert d[22]["DATA"] == 40 * MB
    assert sum(x.get("DATA", 0) for x in d.values()) == state.counters(SUB, "2026-11")["DATA"]


def test_dry_run_writes_nothing_and_unexplained_usage_is_reported(world):
    state, _, key, archive = world
    archive.pop(0)  # the archive is missing one CDR: that usage must stay without day detail, not be invented
    before = state.r.hgetall(key)
    report = bf.backfill_subscriber(state, SUB, apply=False)
    assert state.r.hgetall(key) == before
    assert report[0]["unexplained"] == {"DATA": 100 * MB}
