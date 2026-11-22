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


def test_overlapping_batches_count_a_record_once_and_a_failed_publish_releases_it():
    from usage_collector.screening import release, remember
    r = fakeredis.FakeRedis()
    batch = [rec(i, usage_type="DATA", quantity=100) for i in range(10, 13)]
    first = screen(batch, r)                       # claims the 3 ids
    second = screen(batch, r)                      # concurrent re-send: nothing left to claim
    assert len(first.valid) == 3 and len(second.valid) == 0 and second.duplicates == 3
    release(first.valid, r)                        # first publish failed
    retry = screen(batch, r)
    assert len(retry.valid) == 3                   # the retry goes through
    remember(retry.valid, r)
    assert screen(batch, r).duplicates == 3        # and afterwards it's remembered


def test_add_on_events_only_from_the_dedicated_order_source_once_its_token_exists():
    from usage_collector import app as collector
    collector.S.settings = collector.Settings(order_source_token="", order_sources="emu-mediation-01")
    assert collector.order_sources() == {"emu-mediation-01"}          # older setup: fallback list
    collector.S.settings = collector.Settings(order_source_token="t0k3n")
    assert collector.order_sources() == {"core-orders"}               # dedicated token: only the order workflow
