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
