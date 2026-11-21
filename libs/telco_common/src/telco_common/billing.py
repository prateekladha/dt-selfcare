"""Rating engine: cycle usage counters + plan -> itemised bill.

Input is the flat counter map that the stream aggregator maintains per subscriber per cycle
(e.g. {"DATA": 812345678, "ROAMING_DATA:Z2": 120000000, "addon:DE-DATA-5GB": 1}); the same
function prices both the *unbilled* running total and the final bill at cycle close, so the
number a customer sees mid-month is computed exactly like the invoice.
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from pydantic import BaseModel

from .catalog import COUNTRIES, MB, MESSAGE_RATES, ROAM_LIKE_AT_HOME_ZONE, OveragePolicy, Plan, get_addon
from .models import UsageType


class LineCategory(StrEnum):
    PLAN = "PLAN"
    ADDON = "ADDON"
    OVERAGE = "OVERAGE"
    ROAMING = "ROAMING"
    INTERNATIONAL = "INTERNATIONAL"
    MESSAGING = "MESSAGING"
    THIRD_PARTY = "THIRD_PARTY"
    ADJUSTMENT = "ADJUSTMENT"


class BillLine(BaseModel):
    code: str
    category: LineCategory
    label: str
    quantity: int
    unit: str
    amount: int  # minor units, net


class UsageTotals(BaseModel):
    """Human-readable view of the counters (used on bills and by the explainer)."""

    data_mb: int  # domestic + EU roaming, rounded up
    data_allowance_mb: int
    voice_min: int
    sms: int
    intl_voice_min: int
    roaming_data_mb: dict[str, int]  # non-EU zones
    roaming_voice_min: dict[str, int]
    eu_roaming_data_mb: int
    # added with extended CDR coverage (defaults keep older stored bills readable)
    incoming_min: int = 0
    roaming_incoming_min: dict[str, int] = {}
    mms: int = 0
    intl_sms: int = 0
    free_calls_min: int = 0  # emergency + toll-free
    zero_rated_mb: int = 0
    third_party_charges: int = 0


class Bill(BaseModel):
    subscriber_id: str
    cycle: str
    plan_code: str
    country: str
    currency: str
    lines: list[BillLine]
    subtotal: int
    vat_rate: str
    vat: int
    total: int
    usage: UsageTotals


def _money(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _ceil_div(a: int, b: int) -> int:
    return -(-a // b) if a > 0 else 0


def split_counters(counters: dict[str, int]) -> tuple[dict[str, int], dict[str, dict[str, int]], dict[str, int]]:
    """-> (plain counters, zoned counters {type: {zone: qty}}, addons {code: count})."""
    plain: dict[str, int] = {}
    zoned: dict[str, dict[str, int]] = {}
    addons: dict[str, int] = {}
    for key, val in counters.items():
        val = int(val)
        if key.startswith("addon:"):
            addons[key.removeprefix("addon:")] = val
        elif ":" in key:
            t, zone = key.split(":", 1)
            zoned.setdefault(t, {})[zone] = val
        else:
            plain[key] = val
    return plain, zoned, addons


def rate(subscriber_id: str, cycle: str, plan: Plan, counters: dict[str, int],
         adjustments: list[BillLine] | None = None) -> Bill:
    cfg = COUNTRIES[plan.country]
    plain, zoned, addons = split_counters(counters)
    lines: list[BillLine] = [
        BillLine(code="PLAN_FEE", category=LineCategory.PLAN, label=f"{plan.name} monthly fee",
                 quantity=1, unit="month", amount=plan.monthly_fee)
    ]

    addon_mb = 0
    for code, count in sorted(addons.items()):
        addon = get_addon(code)
        addon_mb += addon.data_mb * count
        lines.append(BillLine(code=f"ADDON:{code}", category=LineCategory.ADDON, label=addon.name,
                              quantity=count, unit="pcs", amount=addon.price * count))

    roam_data = zoned.get(UsageType.ROAMING_DATA, {})
    roam_voice = zoned.get(UsageType.ROAMING_VOICE, {})
    eu_bytes = roam_data.get(ROAM_LIKE_AT_HOME_ZONE, 0)
    eu_voice_s = roam_voice.get(ROAM_LIKE_AT_HOME_ZONE, 0)

    data_bytes = plain.get(UsageType.DATA, 0) + eu_bytes
    allowance_mb = plan.data_mb + addon_mb
    data_mb = _ceil_div(data_bytes, MB)
    over_mb = max(0, data_mb - allowance_mb)
    if over_mb and plan.overage_policy == OveragePolicy.CHARGE:
        lines.append(BillLine(code="DATA_OVERAGE", category=LineCategory.OVERAGE,
                              label="Data beyond allowance", quantity=over_mb, unit="MB",
                              amount=_money(plan.data_overage_per_mb * over_mb)))

    voice_min = _ceil_div(plain.get(UsageType.VOICE, 0) + eu_voice_s, 60)
    if plan.voice_min is not None and voice_min > plan.voice_min:
        q = voice_min - plan.voice_min
        lines.append(BillLine(code="VOICE_OVERAGE", category=LineCategory.OVERAGE,
                              label="Minutes beyond allowance", quantity=q, unit="min",
                              amount=_money(plan.voice_overage_per_min * q)))

    sms = plain.get(UsageType.SMS, 0)
    if plan.sms is not None and sms > plan.sms:
        q = sms - plan.sms
        lines.append(BillLine(code="SMS_OVERAGE", category=LineCategory.OVERAGE,
                              label="SMS beyond allowance", quantity=q, unit="sms",
                              amount=_money(plan.sms_each * q)))

    intl_min = _ceil_div(plain.get(UsageType.INTL_VOICE, 0), 60)
    if intl_min:
        lines.append(BillLine(code="INTL_VOICE", category=LineCategory.INTERNATIONAL,
                              label="International calls", quantity=intl_min, unit="min",
                              amount=_money(plan.intl_voice_per_min * intl_min)))

    roaming_data_mb: dict[str, int] = {}
    roaming_voice_min: dict[str, int] = {}
    for zone, b in sorted(roam_data.items()):
        if zone == ROAM_LIKE_AT_HOME_ZONE or b <= 0:
            continue
        mb = _ceil_div(b, MB)
        roaming_data_mb[zone] = mb
        lines.append(BillLine(code=f"ROAMING_DATA:{zone}", category=LineCategory.ROAMING,
                              label=f"Roaming data, zone {zone}", quantity=mb, unit="MB",
                              amount=_money(plan.roaming[zone].data_per_mb * mb)))
    for zone, s in sorted(roam_voice.items()):
        if zone == ROAM_LIKE_AT_HOME_ZONE or s <= 0:
            continue
        m = _ceil_div(s, 60)
        roaming_voice_min[zone] = m
        lines.append(BillLine(code=f"ROAMING_VOICE:{zone}", category=LineCategory.ROAMING,
                              label=f"Roaming calls, zone {zone}", quantity=m, unit="min",
                              amount=_money(plan.roaming[zone].voice_per_min * m)))

    # incoming calls: free at home; charged per zone when roaming outside the EU
    roam_mt = zoned.get(UsageType.ROAMING_VOICE_MT, {})
    roaming_incoming_min: dict[str, int] = {}
    for zone, s_ in sorted(roam_mt.items()):
        m = _ceil_div(s_, 60)
        roaming_incoming_min[zone] = m
        if zone == ROAM_LIKE_AT_HOME_ZONE or m == 0:
            continue
        lines.append(BillLine(code=f"ROAMING_VOICE_MT:{zone}", category=LineCategory.ROAMING,
                              label=f"Incoming calls while roaming, zone {zone}", quantity=m, unit="min",
                              amount=_money(plan.roaming[zone].voice_mt_per_min * m)))
    rates = MESSAGE_RATES[plan.country]
    mms = plain.get(UsageType.MMS, 0)
    if mms:
        lines.append(BillLine(code="MMS", category=LineCategory.MESSAGING, label="MMS", quantity=mms, unit="mms",
                              amount=_money(rates["MMS"] * mms)))
    intl_sms = plain.get(UsageType.SMS_INTL, 0)
    if intl_sms:
        lines.append(BillLine(code="SMS_INTL", category=LineCategory.MESSAGING, label="International SMS",
                              quantity=intl_sms, unit="sms", amount=_money(rates["SMS_INTL"] * intl_sms)))
    premium = plain.get(UsageType.PREMIUM, 0)
    if premium:
        lines.append(BillLine(code="PREMIUM", category=LineCategory.THIRD_PARTY,
                              label="Third-party services (premium / value-added)", quantity=1, unit="total",
                              amount=premium))
    lines.extend(adjustments or [])

    subtotal = sum(line.amount for line in lines)
    vat = _money(Decimal(subtotal) * cfg.vat_rate)
    return Bill(
        subscriber_id=subscriber_id, cycle=cycle, plan_code=plan.code, country=plan.country,
        currency=cfg.currency, lines=lines, subtotal=subtotal, vat_rate=str(cfg.vat_rate),
        vat=vat, total=subtotal + vat,
        usage=UsageTotals(
            data_mb=data_mb, data_allowance_mb=allowance_mb, voice_min=voice_min, sms=sms,
            intl_voice_min=intl_min, roaming_data_mb=roaming_data_mb,
            roaming_voice_min=roaming_voice_min, eu_roaming_data_mb=_ceil_div(eu_bytes, MB),
            incoming_min=_ceil_div(plain.get(UsageType.VOICE_MT, 0), 60), roaming_incoming_min=roaming_incoming_min,
            mms=mms, intl_sms=intl_sms,
            free_calls_min=_ceil_div(plain.get(UsageType.EMERGENCY, 0) + plain.get(UsageType.TOLLFREE, 0), 60),
            zero_rated_mb=_ceil_div(plain.get(UsageType.DATA_ZERO_RATED, 0), MB), third_party_charges=premium,
        ),
    )


def data_usage_pct(plan: Plan, counters: dict[str, int]) -> float:
    plain, zoned, addons = split_counters(counters)
    used = plain.get(UsageType.DATA, 0) + zoned.get(UsageType.ROAMING_DATA, {}).get(ROAM_LIKE_AT_HOME_ZONE, 0)
    allowance = plan.data_allowance_bytes + sum(get_addon(c).data_mb * n for c, n in addons.items()) * MB
    return math.inf if allowance == 0 else used * 100 / allowance
