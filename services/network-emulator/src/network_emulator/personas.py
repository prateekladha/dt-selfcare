"""Deterministic usage generation per persona and day.

Seeded by (subscriber, date) so a backfill can be re-run and produces byte-identical batches
(same batch ids) -> the collector answers "duplicate" and nothing is double counted.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

from telco_common.catalog import MB

# share of the plan's monthly data allowance a persona uses in a normal month
MONTH_SHARE = {"streamer": 0.55, "traveller": 0.85, "intl_caller": 0.7, "addon_buyer": 0.85, "light": 0.38}


def _rng(sub: str, d: date, salt: str = "") -> random.Random:
    return random.Random(f"{sub}|{d.isoformat()}|{salt}")


def day_records(sub: str, country: str, persona: str, allowance_mb: int, d: date, today: date) -> list[dict]:
    """All records for one subscriber-day, as JSON-ready dicts (fast path, no pydantic)."""
    rng = _rng(sub, d)
    months_ago = (today.year - d.year) * 12 + today.month - d.month
    growth = 1.0
    if persona == "streamer":
        growth = 1.0 + 0.085 * (11 - min(months_ago, 11))  # usage grows every month -> 80%/100% alerts
    if persona == "addon_buyer" and d.month in (3, 6, 9, 11):
        growth = 1.75  # heavy months -> buys add-on instead of paying overage
    out: list[dict] = []
    seq = 0

    def rec(**kw) -> None:
        nonlocal seq
        hour, minute = rng.randint(6, 23), rng.randint(0, 59)
        ts = datetime(d.year, d.month, d.day, hour, minute, rng.randint(0, 59), tzinfo=UTC)
        out.append({"record_id": f"{sub}-{d:%Y%m%d}-{seq:04d}", "subscriber_id": sub, "country": country,
                    "event_time": ts.isoformat(), **kw})
        seq += 1

    roaming_zone = None
    if persona == "traveller" and d.month in (7, 10) and 8 <= d.day <= 20:
        roaming_zone = "Z2"
    elif persona == "traveller" and d.month == 8 and 1 <= d.day <= 14:
        roaming_zone = "EU"

    sessions = rng.randint(3, 8)
    day_bytes = int(allowance_mb * MONTH_SHARE[persona] / 30 * growth * rng.uniform(0.6, 1.4) * MB)
    sid = f"{sub}-{d:%Y%m%d}-s"
    for k in range(sessions):
        q = day_bytes // sessions
        if not roaming_zone and k == 0 and sessions > 3:  # one long session reported as 3 interim records
            for part in range(3):
                rec(usage_type="DATA", quantity=q // 3, session_id=f"{sid}0", sequence=part)
            continue
        if roaming_zone:
            rec(usage_type="ROAMING_DATA", roaming_zone=roaming_zone, quantity=q // (3 if roaming_zone == "Z2" else 1))
        else:
            rec(usage_type="DATA", quantity=q)

    for _ in range(rng.randint(1, 4)):
        secs = rng.randint(20, 240)
        if roaming_zone == "Z2" and rng.random() < 0.4:
            rec(usage_type="ROAMING_VOICE", roaming_zone="Z2", quantity=secs)
        else:
            rec(usage_type="VOICE", quantity=secs)
    if persona == "intl_caller" and rng.random() < (0.25 if d.month != 8 else 0.6):
        rec(usage_type="INTL_VOICE", quantity=rng.randint(60, 700))
    for _ in range(rng.randint(0, 3)):
        rec(usage_type="SMS", quantity=1)

    # ---- extended CDR coverage (realistic frequencies) ----
    for _ in range(rng.randint(0, 3)):  # incoming calls: free at home, charged when roaming outside the EU
        secs = rng.randint(15, 300)
        if roaming_zone:
            rec(usage_type="ROAMING_VOICE_MT", roaming_zone=roaming_zone, quantity=secs, session_id=f"mt-{seq}")
        else:
            rec(usage_type="VOICE_MT", quantity=secs)
    if rng.random() < 0.08:
        rec(usage_type="MMS", quantity=1)
    if rng.random() < (0.3 if persona == "intl_caller" else 0.03):
        rec(usage_type="SMS_INTL", quantity=1)
    if rng.random() < 0.05:
        rec(usage_type="TOLLFREE", quantity=rng.randint(60, 600))
    if rng.random() < 0.004:
        rec(usage_type="EMERGENCY", quantity=rng.randint(30, 240))
    if rng.random() < 0.03:  # premium SMS / value-added service, price set by the provider
        rec(usage_type="PREMIUM", quantity=rng.choice([99, 199, 299, 499]), service=rng.choice(["vas-quiz", "vas-ringtone", "donation-ngo"]))
    if persona == "streamer" and rng.random() < 0.6:  # zero-rated streaming pass: no allowance use
        rec(usage_type="DATA_ZERO_RATED", quantity=int(rng.uniform(200, 900) * MB), service="stream-pass")
    if rng.random() < 0.05:  # dropped / zero-duration call attempt
        rec(usage_type="VOICE", quantity=0)

    if persona == "addon_buyer" and d.month in (3, 6, 9, 11) and d.day == 18:
        rec(kind="PLAN_CHANGED", addon_code="DE-DATA-5GB")
    return out


def days_between(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def synthetic_record(rng: random.Random, n_subscribers: int, seq: int, now: datetime) -> dict:
    """Load-test record for a large synthetic population (no profile -> counted, never alerted)."""
    i = rng.randrange(n_subscribers)
    sub = f"4917{i:09d}"
    t = rng.random()
    if t < 0.75:
        kw = {"usage_type": "DATA", "quantity": rng.randint(50_000, 40 * MB)}
    elif t < 0.95:
        kw = {"usage_type": "VOICE", "quantity": rng.randint(10, 900)}
    else:
        kw = {"usage_type": "SMS", "quantity": 1}
    return {"record_id": f"lt-{now:%Y%m%d%H%M%S}-{seq:010d}", "subscriber_id": sub, "country": "DE",
            "event_time": now.isoformat(), **kw}
