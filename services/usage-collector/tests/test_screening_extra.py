"""Values the rating engine doesn't know are quarantined at ingest (they used to fail downstream)."""
from datetime import UTC, datetime

import fakeredis
from usage_collector.screening import screen

T = datetime.now(UTC).isoformat()


def rec(i, **kw):
    return {"record_id": f"rec-{i:06d}", "subscriber_id": "4915100000001", "country": "DE", "event_time": T, **kw}


def test_unknown_addon_zone_and_huge_quantity_are_quarantined():
    out = screen([rec(1, kind="PLAN_CHANGED", addon_code="DE-DATA-5G"),            # typo: crash-looped the aggregator
                  rec(2, usage_type="ROAMING_DATA", roaming_zone="Z1", quantity=10),  # unknown zone: broke the bill run
                  rec(3, usage_type="DATA", quantity=10**18),                         # overflowed HINCRBY mid-message
                  rec(4, usage_type="ROAMING_DATA", roaming_zone="Z2", quantity=10),
                  rec(5, kind="PLAN_CHANGED", addon_code="DE-DATA-5GB")], fakeredis.FakeRedis())
    reasons = [q["reason"] for q in out.quarantined]
    assert len(out.valid) == 2 and len(reasons) == 3
    assert any("add-on" in r for r in reasons) and any("roaming zone" in r for r in reasons) and any("large" in r for r in reasons)
