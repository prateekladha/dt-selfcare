from datetime import UTC, datetime

import pytest
from telco_common.billing import data_usage_pct, rate
from telco_common.catalog import MB, get_plan
from telco_common.codec import decode_records, encode_records, message_id
from telco_common.explainer import explain
from telco_common.models import Country, EventKind, UsageRecord, UsageType
from telco_common.partitioning import partition_for


def test_plan_only_bill_adds_vat():
    bill = rate("s1", "2026-10", get_plan("DE-M"), {})
    assert [line.code for line in bill.lines] == ["PLAN_FEE"]
    assert bill.subtotal == 3495
    assert bill.vat == 664  # 19% of 34.95 = 6.6405 -> 6.64
    assert bill.total == 4159
    assert bill.currency == "EUR"


def test_flex_plan_charges_overage_and_rounds_up_partial_units():
    plan = get_plan("DE-FLEX")  # 3 GB, 200 min, 100 sms
    counters = {"DATA": 3_000 * MB + 1, "VOICE": 200 * 60 + 61, "SMS": 105}
    bill = rate("s1", "2026-10", plan, counters)
    lines = {line.code: line for line in bill.lines}
    assert lines["DATA_OVERAGE"].quantity == 1  # 1 byte over -> 1 started MB
    assert lines["DATA_OVERAGE"].amount == 1  # 0.9 cent -> rounds half-up to 1
    assert lines["VOICE_OVERAGE"].quantity == 2  # 61 s over -> 2 minutes
    assert lines["SMS_OVERAGE"].amount == 45


def test_throttle_plan_never_charges_data_overage():
    bill = rate("s1", "2026-10", get_plan("DE-S"), {"DATA": 50_000 * MB})
    assert "DATA_OVERAGE" not in {line.code for line in bill.lines}


def test_eu_roaming_is_roam_like_at_home_but_zone2_is_charged():
    plan = get_plan("DE-FLEX")
    bill = rate("s1", "2026-10", plan, {"ROAMING_DATA:EU": 500 * MB, "ROAMING_DATA:Z2": 100 * MB,
                                        "ROAMING_VOICE:Z2": 90})
    lines = {line.code: line for line in bill.lines}
    assert "ROAMING_DATA:EU" not in lines
    assert bill.usage.data_mb == 500  # EU roaming counted against domestic allowance
    assert lines["ROAMING_DATA:Z2"].amount == 199  # 100 MB * 1.99
    assert lines["ROAMING_VOICE:Z2"].quantity == 2


def test_addon_extends_allowance_and_is_billed():
    plan = get_plan("DE-FLEX")
    counters = {"DATA": 7_000 * MB, "addon:DE-DATA-5GB": 1}
    bill = rate("s1", "2026-10", plan, counters)
    codes = {line.code for line in bill.lines}
    assert "ADDON:DE-DATA-5GB" in codes and "DATA_OVERAGE" not in codes
    assert bill.usage.data_allowance_mb == 8_000
    assert data_usage_pct(plan, counters) == pytest.approx(87.5)


def test_poland_uses_pln_and_23_percent_vat():
    bill = rate("s1", "2026-10", get_plan("PL-S"), {})
    assert bill.currency == "PLN" and bill.vat == 805


def test_explainer_ranks_drivers_by_impact():
    plan = get_plan("DE-FLEX")
    prev = rate("s1", "2026-09", plan, {"DATA": 1_000 * MB})
    cur = rate("s1", "2026-10", plan, {"DATA": 3_500 * MB, "ROAMING_DATA:Z2": 1_000 * MB, "INTL_VOICE": 600})
    ex = explain(cur, prev)
    assert ex.direction == "UP"
    assert ex.drivers[0].code == "ROAMING_DATA:Z2"  # 19.90 > 4.50 overage > 3.90 intl
    assert ex.delta_total == cur.total - prev.total
    assert ex.facts[0].startswith("Your bill increased by")
    assert ex.top_usage_drivers[0].share_pct > 50


def test_explainer_first_bill():
    ex = explain(rate("s1", "2026-10", get_plan("DE-S"), {}), None)
    assert ex.direction == "FIRST_BILL" and ex.drivers == []


def _rec(i: int, sub: str = "4915100000001") -> UsageRecord:
    return UsageRecord(record_id=f"rec-{i:08d}", subscriber_id=sub, country=Country.DE,
                       usage_type=UsageType.DATA, quantity=1000 * i,
                       event_time=datetime(2026, 11, 3, tzinfo=UTC))


def test_codec_roundtrip_and_compression():
    recs = [_rec(i) for i in range(500)]
    payload = encode_records(recs)
    assert decode_records(payload) == recs
    raw = sum(len(r.model_dump_json()) for r in recs)
    assert len(payload) * 4 < raw  # at least 4x smaller on the wire


def test_message_id_is_deterministic_and_partitioning_is_stable():
    assert message_id("b1", 3, 0) == message_id("b1", 3, 0) != message_id("b1", 3, 1)
    assert partition_for("4915100000001", 4) == partition_for("4915100000001", 4)


def test_validation_rules():
    with pytest.raises(ValueError):
        UsageRecord(record_id="rec-00000001", subscriber_id="491", country="DE", usage_type="ROAMING_DATA",
                    quantity=1, event_time=datetime(2026, 11, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        UsageRecord(record_id="rec-00000001", subscriber_id="491", country="DE", kind=EventKind.PLAN_CHANGED,
                    event_time=datetime(2026, 11, 1, tzinfo=UTC))
    with pytest.raises(ValueError):
        UsageRecord(record_id="rec-00000001", subscriber_id="491", country="DE", usage_type="DATA",
                    quantity=1, event_time=datetime(2026, 11, 1))
