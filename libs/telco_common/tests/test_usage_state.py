from datetime import UTC, datetime

import fakeredis
import pytest
from telco_common.catalog import MB
from telco_common.models import Country, EventKind, UsageRecord, UsageType
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState, outbox_key

P = 4
LEVELS = [50, 80, 100]
SUB = "4915100000001"  # DE-FLEX: 3,000 MB allowance
PART = partition_for(SUB, P)
T = datetime(2026, 11, 10, tzinfo=UTC)


@pytest.fixture
def state():
    s = UsageState(fakeredis.FakeRedis(), P)
    s.upsert_profile(SUB, "DE-FLEX")
    return s


def data(i: int, mb: int, zone: str | None = None) -> UsageRecord:
    return UsageRecord(record_id=f"r-{i:08d}", subscriber_id=SUB, country=Country.DE,
                       usage_type=UsageType.ROAMING_DATA if zone else UsageType.DATA,
                       roaming_zone=zone, quantity=mb * MB, event_time=T)


def alerts(s: UsageState) -> list[dict]:
    return [{k.decode(): v.decode() for k, v in e[1].items()} for e in s.r.xrange(outbox_key(PART))]


def test_crossing_80_raises_exactly_one_alert(state):
    assert state.apply(PART, 0, "m0", [data(0, 2_000)], LEVELS).alerts == 1  # 66% -> 50 alert
    assert state.apply(PART, 1, "m1", [data(1, 450)], LEVELS).alerts == 1  # 81% -> 80 alert
    assert state.apply(PART, 2, "m2", [data(2, 10)], LEVELS).alerts == 0  # still 81%, no repeat
    got = alerts(state)
    assert [a["level"] for a in got] == ["50", "80"]
    assert got[1]["alert_id"] == f"{SUB}:2026-11:80"


def test_replayed_offset_after_crash_is_not_double_counted(state):
    state.apply(PART, 5, "m5", [data(0, 100)], LEVELS)
    r = state.apply(PART, 5, "m5", [data(0, 100)], LEVELS)  # consumer restarted from older offset
    assert (r.applied, r.reason) == (False, "stale_offset")
    assert state.counters(SUB, "2026-11")["DATA"] == 100 * MB


def test_republished_message_at_new_offset_is_dropped(state):
    state.apply(PART, 5, "same-msg", [data(0, 100)], LEVELS)
    r = state.apply(PART, 9, "same-msg", [data(0, 100)], LEVELS)  # collector retried the batch
    assert r.reason == "duplicate_message"
    assert state.counters(SUB, "2026-11")["DATA"] == 100 * MB
    assert state.committed_offset(PART) == 9  # offset still advances


def test_eu_roaming_counts_toward_allowance_but_zone2_does_not(state):
    state.apply(PART, 0, "m0", [data(0, 2_500, "EU"), data(1, 5_000, "Z2")], LEVELS)
    assert [a["level"] for a in alerts(state)] == ["50", "80"]


def test_addon_rearms_alerts_for_new_allowance(state):
    state.apply(PART, 0, "m0", [data(0, 2_500)], LEVELS)  # 83% of 3 GB -> 50, 80
    addon = UsageRecord(record_id="r-addon-01", subscriber_id=SUB, country=Country.DE,
                        kind=EventKind.PLAN_CHANGED, addon_code="DE-DATA-5GB", event_time=T)
    state.apply(PART, 1, "m1", [addon], LEVELS)  # 2.5 of 8 GB = 31% -> re-arm 50/80
    state.apply(PART, 2, "m2", [data(1, 4_000)], LEVELS)  # 6.5 of 8 GB = 81%
    assert [a["level"] for a in alerts(state)] == ["50", "80", "50", "80"]
    assert state.counters(SUB, "2026-11")["addon:DE-DATA-5GB"] == 1


def test_unknown_subscriber_counts_usage_without_alerting(state):
    rec = data(0, 99_999).model_copy(update={"subscriber_id": "4915199999999"})
    part = partition_for(rec.subscriber_id, P)
    assert state.apply(part, 0, "m0", [rec], LEVELS).alerts == 0


def test_record_on_wrong_partition_is_rejected(state):
    with pytest.raises(ValueError):
        state.apply((PART + 1) % P, 0, "m0", [data(0, 1)], LEVELS)


def test_new_cycle_starts_fresh(state):
    state.apply(PART, 0, "m0", [data(0, 2_900)], LEVELS)
    dec = data(1, 100).model_copy(update={"event_time": datetime(2026, 12, 1, tzinfo=UTC)})
    state.apply(PART, 1, "m1", [dec], LEVELS)
    assert state.counters(SUB, "2026-12")["DATA"] == 100 * MB
