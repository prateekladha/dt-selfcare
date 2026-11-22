from datetime import UTC, datetime

import fakeredis
from network_emulator.personas import day_records
from network_emulator.scenarios import build
from telco_common.models import UsageRecord
from usage_collector.screening import screen


def test_every_scenario_is_handled_as_documented():
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    r = fakeredis.FakeRedis()
    results = {}
    for key, _desc, recs in build(now):
        sc = screen(recs, r, now)
        from usage_collector.screening import remember
        remember(sc.valid, r)
        results[key] = (len(sc.valid), len(sc.quarantined), sc.duplicates)
    assert results["malformed"] == (1, 3, 0)
    assert results["future-dated"] == (0, 1, 0)
    assert results["duplicate-record"] == (1, 0, 1)
    assert results["duplicate-record-resent"] == (0, 0, 1)
    assert results["data-session-interim"] == (3, 0, 0)
    assert results["late-cdr"][0] == 1


def test_personas_emit_extended_cdr_types_that_validate():
    types = set()
    for day in range(1, 29):
        for rec in day_records("4915100000001", "DE", "streamer", 30_000, datetime(2026, 11, day).date(),
                               datetime(2026, 11, 28).date()):
            u = UsageRecord.model_validate(rec)
            types.add(str(u.usage_type))
    assert {"VOICE_MT", "DATA_ZERO_RATED", "DATA", "VOICE"} <= types


def test_live_traffic_runs_at_the_personas_daily_pace():
    """Live emission releases each deterministic CDR once, at its event time (not 72x a day)."""
    from datetime import UTC, datetime, timedelta

    from network_emulator.cli import due_records
    from network_emulator.personas import day_records
    from telco_common.catalog import get_plan
    from telco_common.demo import DEMO_SUBSCRIBERS

    day = datetime(2026, 11, 22, tzinfo=UTC)
    ids: list[str] = []
    t = day
    while t < day + timedelta(days=1):
        ids += [r["record_id"] for r in due_records(t, t + timedelta(minutes=1))]
        t += timedelta(minutes=1)
    expected = [r["record_id"] for s in DEMO_SUBSCRIBERS
                for r in day_records(s.subscriber_id, s.country, s.persona, get_plan(s.plan_code).data_mb, day.date(), day.date())]
    assert sorted(ids) == sorted(expected) and len(ids) == len(set(ids))
