"""Product catalogue and per-country configuration.

In production this is owned by the Product Catalogue domain (TM Forum TMF620) and served
from its API; here it is versioned config so tariff changes go through code review.
Amounts are in *minor units* (cents / grosze) and exclude VAT. Per-unit rates are Decimals
because rates like 0.9 cent/MB are common.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from .models import Country

MB = 1_000_000  # bytes; decimal MB as used on consumer bills


class OveragePolicy(StrEnum):
    CHARGE = "CHARGE"  # bill per MB beyond allowance
    THROTTLE = "THROTTLE"  # slow down at 100%, no extra charge


# EU "roam like at home" regulation: usage in zone EU consumes the domestic allowance.
ROAM_LIKE_AT_HOME_ZONE = "EU"


@dataclass(frozen=True)
class RoamingRates:
    data_per_mb: Decimal
    voice_per_min: Decimal
    voice_mt_per_min: Decimal = Decimal(0)  # receiving calls while roaming in this zone


@dataclass(frozen=True)
class Plan:
    code: str
    country: Country
    name: str
    monthly_fee: int
    data_mb: int
    voice_min: int | None  # None = unlimited
    sms: int | None
    data_overage_per_mb: Decimal
    voice_overage_per_min: Decimal
    sms_each: Decimal
    intl_voice_per_min: Decimal
    overage_policy: OveragePolicy
    roaming: dict[str, RoamingRates] = field(default_factory=dict)

    @property
    def data_allowance_bytes(self) -> int:
        return self.data_mb * MB


@dataclass(frozen=True)
class CountryConfig:
    country: Country
    currency: str
    vat_rate: Decimal
    locale: str
    timezone: str


COUNTRIES: dict[Country, CountryConfig] = {
    Country.DE: CountryConfig(Country.DE, "EUR", Decimal("0.19"), "de-DE", "Europe/Berlin"),
    Country.PL: CountryConfig(Country.PL, "PLN", Decimal("0.23"), "pl-PL", "Europe/Warsaw"),
}

_DE_ROAMING = {
    "Z2": RoamingRates(Decimal("1.99"), Decimal(49), Decimal(19)),  # CH, UK, US …
    "Z3": RoamingRates(Decimal("9.99"), Decimal(199), Decimal(99)),  # rest of world
}
_PL_ROAMING = {
    "Z2": RoamingRates(Decimal("8.6"), Decimal(199), Decimal(79)),
    "Z3": RoamingRates(Decimal(43), Decimal(899), Decimal(399)),
}

# Per-country message rates (minor units, net): MMS and international SMS are outside any SMS allowance.
MESSAGE_RATES = {Country.DE: {"MMS": Decimal(39), "SMS_INTL": Decimal(19)},
                 Country.PL: {"MMS": Decimal(49), "SMS_INTL": Decimal(49)}}

PLANS: dict[str, Plan] = {
    p.code: p
    for p in [
        Plan("DE-S", Country.DE, "Mobile S", 1995, 10_000, None, None,
             Decimal(0), Decimal(0), Decimal(0), Decimal(29), OveragePolicy.THROTTLE, _DE_ROAMING),
        Plan("DE-M", Country.DE, "Mobile M", 3495, 30_000, None, None,
             Decimal(0), Decimal(0), Decimal(0), Decimal(19), OveragePolicy.THROTTLE, _DE_ROAMING),
        Plan("DE-FLEX", Country.DE, "Mobile Flex", 995, 3_000, 200, 100,
             Decimal("0.9"), Decimal(9), Decimal(9), Decimal(39), OveragePolicy.CHARGE, _DE_ROAMING),
        Plan("PL-S", Country.PL, "Mobile S", 3500, 15_000, None, None,
             Decimal(0), Decimal(0), Decimal(0), Decimal(99), OveragePolicy.THROTTLE, _PL_ROAMING),
        Plan("PL-FLEX", Country.PL, "Mobile Flex", 2500, 5_000, 300, 300,
             Decimal("3.5"), Decimal(29), Decimal(19), Decimal(149), OveragePolicy.CHARGE, _PL_ROAMING),
    ]
}


def get_plan(code: str) -> Plan:
    try:
        return PLANS[code]
    except KeyError as e:
        raise KeyError(f"unknown plan {code!r}") from e


def plans_for(country: Country) -> list[Plan]:
    return [p for p in PLANS.values() if p.country == country]


@dataclass(frozen=True)
class AddOn:
    code: str
    country: Country
    name: str
    data_mb: int
    price: int  # minor units, net


ADDONS: dict[str, AddOn] = {
    a.code: a
    for a in [
        AddOn("DE-DATA-5GB", Country.DE, "5 GB data add-on", 5_000, 999),
        AddOn("DE-DATA-20GB", Country.DE, "20 GB data add-on", 20_000, 1999),
        AddOn("PL-DATA-5GB", Country.PL, "5 GB data add-on", 5_000, 2500),
    ]
}


def get_addon(code: str) -> AddOn:
    try:
        return ADDONS[code]
    except KeyError as e:
        raise KeyError(f"unknown add-on {code!r}") from e
