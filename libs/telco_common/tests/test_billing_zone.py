"""An unknown roaming zone must not raise inside rating (it stopped the whole bill run)."""
from telco_common.billing import rate
from telco_common.catalog import MB, get_plan


def test_unknown_zone_is_priced_as_the_most_expensive_zone():
    plan = get_plan("DE-FLEX")
    unknown = rate("4915100000001", "2026-10", plan, {"ROAMING_DATA:Z9": 10 * MB})
    worst = max(plan.roaming, key=lambda z: plan.roaming[z].data_per_mb)
    known = rate("4915100000001", "2026-10", plan, {f"ROAMING_DATA:{worst}": 10 * MB})
    assert unknown.subtotal == known.subtotal > 0
