"""Evidence trail: raw CDRs in the archive are re-summed and reconciled with Valkey (and mismatches surface)."""
import gzip
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import fakeredis
from core_api.services import ai, evidence
from telco_common.billing import rate
from telco_common.catalog import MB, get_plan
from telco_common.codec import encode_records
from telco_common.explainer import explain
from telco_common.models import Country, UsageRecord, UsageType
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState

SUB = "4915100000002"
T = datetime(2026, 10, 12, tzinfo=UTC)


def recs():
    return [UsageRecord(record_id=f"r-{i:06d}", subscriber_id=SUB, country=Country.DE, usage_type=t, roaming_zone=z,
                        quantity=q, event_time=T) for i, (t, z, q) in enumerate([
        (UsageType.DATA, None, 300 * MB), (UsageType.ROAMING_DATA, "Z2", 120 * MB),
        (UsageType.ROAMING_DATA, "Z2", 80 * MB), (UsageType.VOICE, None, 95)])]


class FakeOS:
    def __init__(self, objects):
        self.objects = objects

    def list_objects(self, ns, bucket, prefix, start=None, fields=None):
        objs = [SimpleNamespace(name=n) for n in self.objects if n.startswith(prefix)]
        return SimpleNamespace(data=SimpleNamespace(objects=objs, next_start_with=None))

    def get_object(self, ns, bucket, name):
        return SimpleNamespace(data=SimpleNamespace(content=self.objects[name]))


def setup(monkeypatch, archive_records, applied_records):
    p = partition_for(SUB, 4)
    other = (p + 1) % 4
    body = encode_records(archive_records)
    dup = encode_records(archive_records[:1])  # at-least-once archive: an overlapping re-upload
    objects = {f"raw/usage-events/dt=2026-11-21/p={p}/0-9.jsonl.gz": body + dup,
               f"raw/usage-events/dt=2026-11-21/p={other}/0-9.jsonl.gz": gzip.compress(b"")}
    monkeypatch.setattr(evidence, "_client", lambda: (FakeOS(objects), "ns"))
    state = UsageState(fakeredis.FakeRedis(), 4)
    state.apply(p, 0, "m0", applied_records, [50, 80, 100])
    return state


def test_cdrs_reconcile_with_valkey_and_duplicates_are_ignored(monkeypatch):
    state = setup(monkeypatch, recs(), recs())
    ev = evidence.cdr_evidence(state, SUB, "2026-10")
    assert ev["cdr_count"] == 4 and ev["all_match"]
    z2 = next(r for r in ev["reconciliation"] if r["key"] == "ROAMING_DATA:Z2")
    assert (z2["cdr_records"], z2["from_cdrs"], z2["valkey_counter"]) == (2, 200 * MB, 200 * MB)
    assert {s["record_id"] for s in ev["sample"]} >= {"r-000001", "r-000002"}


def test_mismatch_between_archive_and_counters_is_visible(monkeypatch):
    state = setup(monkeypatch, recs(), recs()[:3])  # one CDR never reached the counters
    ev = evidence.cdr_evidence(state, SUB, "2026-10")
    assert not ev["all_match"]
    voice = next(r for r in ev["reconciliation"] if r["key"] == "VOICE")
    assert voice["from_cdrs"] == 95 and voice["valkey_counter"] == 0


def test_citations_must_reference_real_sources():
    plan = get_plan("DE-FLEX")
    ex = explain(rate("s", "2026-10", plan, {"DATA": 3_500 * MB}), rate("s", "2026-09", plan, {"DATA": 1_000 * MB}))
    n = len(ex.facts) + len(ex.usage_changes)
    ok = f"Your bill went up [1]. Data use rose [{n}]."
    assert ai.grounded(ok, ex.facts, [*ex.usage_changes])
    assert not ai.grounded("Your bill went up [42].", ex.facts, [*ex.usage_changes])
    json.dumps(ex.model_dump())  # serialisable for the sources panel
