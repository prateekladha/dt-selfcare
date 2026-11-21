"""Edge-case CDR scenarios, each sent as its own batch so the collector's per-batch response
(accepted / quarantined / duplicates) shows how the platform handled it. Targets the lab line.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from telco_common.catalog import MB

LAB = "4915100000099"


def _r(tag: str, t: datetime, **kw) -> dict:
    return {"record_id": f"lab-{tag}-{uuid.uuid4().hex[:12]}", "subscriber_id": LAB, "country": "DE",
            "event_time": t.isoformat(), **kw}


def build(now: datetime | None = None) -> list[tuple[str, str, list[dict]]]:
    now = now or datetime.now(UTC)
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month = (first - timedelta(days=1)).replace(hour=22)
    dup = _r("dup", now, usage_type="SMS", quantity=1)
    s = []
    s.append(("data-session-interim", "Long data session as 3 interim records (same session id, sequence 0-2)",
              [_r("int", now - timedelta(minutes=30 - 10 * i), usage_type="DATA", quantity=120 * MB, session_id="lab-s1", sequence=i)
               for i in range(3)]))
    s.append(("voice-mo-mt", "Outgoing call 3m20s + incoming call 5m (incoming is free at home)",
              [_r("mo", now, usage_type="VOICE", quantity=200), _r("mt", now, usage_type="VOICE_MT", quantity=300)]))
    s.append(("zero-duration-call", "Dropped call attempt (0 seconds)", [_r("zero", now, usage_type="VOICE", quantity=0)]))
    s.append(("messaging", "SMS, MMS and an international SMS",
              [_r("sms", now, usage_type="SMS", quantity=1), _r("mms", now, usage_type="MMS", quantity=1),
               _r("isms", now, usage_type="SMS_INTL", quantity=1)]))
    s.append(("free-numbers", "Emergency call (112) and toll-free call: always free",
              [_r("sos", now, usage_type="EMERGENCY", quantity=95), _r("tf", now, usage_type="TOLLFREE", quantity=240)]))
    s.append(("premium-vas", "Premium SMS from a value-added service (provider price 2.99)",
              [_r("vas", now, usage_type="PREMIUM", quantity=299, service="vas-quiz")]))
    s.append(("zero-rated", "1.5 GB on a zero-rated streaming pass (no allowance use)",
              [_r("zr", now, usage_type="DATA_ZERO_RATED", quantity=1500 * MB, service="stream-pass")]))
    s.append(("roaming-eu", "EU roaming: 400 MB data + 10 min call (roam like at home)",
              [_r("reu", now, usage_type="ROAMING_DATA", roaming_zone="EU", quantity=400 * MB),
               _r("reuv", now, usage_type="ROAMING_VOICE", roaming_zone="EU", quantity=600)]))
    s.append(("roaming-z2", "Zone 2 roaming: 50 MB data, 4 min outgoing, 6 min incoming",
              [_r("rz2", now, usage_type="ROAMING_DATA", roaming_zone="Z2", quantity=50 * MB),
               _r("rz2v", now, usage_type="ROAMING_VOICE", roaming_zone="Z2", quantity=240),
               _r("rz2mt", now, usage_type="ROAMING_VOICE_MT", roaming_zone="Z2", quantity=360)]))
    s.append(("international", "International call 7 min from home", [_r("intl", now, usage_type="INTL_VOICE", quantity=420)]))
    s.append(("addon", "Customer buys a 5 GB data add-on (allowance grows, alerts re-arm)",
              [_r("add", now, kind="PLAN_CHANGED", addon_code="DE-DATA-5GB")]))
    s.append(("cross-midnight-cycle", "Session spanning the cycle boundary: part before and after midnight on the 1st",
              [_r("x1", first - timedelta(minutes=5), usage_type="DATA", quantity=30 * MB, session_id="lab-s2", sequence=0),
               _r("x2", first + timedelta(minutes=5), usage_type="DATA", quantity=30 * MB, session_id="lab-s2", sequence=1)]))
    s.append(("out-of-order", "Records arriving in reverse event-time order",
              [_r("ooo", now - timedelta(minutes=m), usage_type="SMS", quantity=1) for m in (1, 20, 40)]))
    s.append(("late-cdr", "CDR for last month arriving now (after that bill) -> adjustment on the next bill",
              [_r("late", last_month, usage_type="ROAMING_DATA", roaming_zone="Z2", quantity=25 * MB)]))
    s.append(("duplicate-record", "Same CDR sent twice in one file and again in a later file",
              [dup, dict(dup)]))
    s.append(("duplicate-record-resent", "The same CDR re-sent in a new file", [dict(dup)]))
    s.append(("malformed", "Unknown usage type + roaming record without zone + negative quantity (quarantined)",
              [_r("bad1", now, usage_type="TELEPORT", quantity=1), _r("bad2", now, usage_type="ROAMING_DATA", quantity=1),
               _r("bad3", now, usage_type="DATA", quantity=-5), _r("ok", now, usage_type="SMS", quantity=1)]))
    s.append(("future-dated", "Event time 2 days in the future (clock skew, quarantined)",
              [_r("fut", now + timedelta(days=2), usage_type="DATA", quantity=MB)]))
    s.append(("unknown-subscriber", "CDR for a number that is not a customer here (counted, no alert)",
              [{**_r("unk", now, usage_type="DATA", quantity=MB), "subscriber_id": "4915199999999"}]))
    return s
