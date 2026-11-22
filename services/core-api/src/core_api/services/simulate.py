"""Usage upload / scenario simulator ("what would this usage cost me, and why?").

The customer either uploads a usage file (CSV or JSON) or picks a scenario. It is priced by the
SAME rating engine as real bills and explained against their latest real bill. It never writes to
the real usage pipeline, so a customer cannot alter their own invoice through this path.

CSV format (header required):  usage_type,quantity,unit,roaming_zone
  DATA,2.5,GB,          VOICE,120,min,        ROAMING_DATA,800,MB,Z2      SMS,10,count,
"""
from __future__ import annotations

import csv
import io
import json
from collections import defaultdict

from telco_common.catalog import MB
from telco_common.models import UsageType

UNIT_FACTORS = {
    ("data", "b"): 1, ("data", "kb"): 1_000, ("data", "mb"): MB, ("data", "gb"): 1_000 * MB,
    ("time", "s"): 1, ("time", "sec"): 1, ("time", "min"): 60, ("time", "h"): 3600,
    ("count", "count"): 1, ("count", "sms"): 1,
}
KIND = {UsageType.DATA: "data", UsageType.ROAMING_DATA: "data", UsageType.VOICE: "time",
        UsageType.ROAMING_VOICE: "time", UsageType.INTL_VOICE: "time", UsageType.SMS: "count"}
MAX_ROWS = 10_000

# mode "add": extras on top of your real month (a trip adds roaming, it doesn't add a second month at home);
# mode "replace": the listed usage REPLACES that part of your real month (a light month is lighter, not extra).
SCENARIOS: dict[str, dict] = {
    "us-trip-2-weeks": {"title": "2 weeks in the US", "mode": "add", "rows": [
        ("ROAMING_DATA", 1.5, "GB", "Z2"), ("ROAMING_VOICE", 90, "min", "Z2")]},
    "eu-summer-holiday": {"title": "3 weeks in Spain (EU)", "mode": "add", "rows": [
        ("ROAMING_DATA", 6, "GB", "EU"), ("ROAMING_VOICE", 150, "min", "EU")]},
    "heavy-streaming": {"title": "Heavy streaming month", "mode": "replace", "rows": [("DATA", 45, "GB", None)]},
    "calling-family-abroad": {"title": "Calling family abroad", "mode": "add", "rows": [("INTL_VOICE", 400, "min", None)]},
    "light-month": {"title": "Light month", "mode": "replace", "rows": [("DATA", 1, "GB", None), ("VOICE", 60, "min", None)]},
}


class UsageFileError(ValueError):
    pass


def _counters(rows: list[tuple[str, float, str, str | None]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    if len(rows) > MAX_ROWS:
        raise UsageFileError(f"too many rows (max {MAX_ROWS})")
    supported = ", ".join(sorted(str(u) for u in KIND))
    for i, (t, qty, unit, zone) in enumerate(rows, start=1):
        try:
            ut = UsageType(str(t).strip().upper())
        except ValueError as e:
            raise UsageFileError(f"row {i}: unknown usage type '{t}'. Supported: {supported}") from e
        if ut not in KIND:
            raise UsageFileError(f"row {i}: '{ut.value}' can't be simulated here. Supported: {supported}")
        factor = UNIT_FACTORS.get((KIND[ut], str(unit or "").strip().lower()))
        if factor is None:
            raise UsageFileError(f"row {i}: unit '{unit}' doesn't fit {ut.value} (data: MB/GB, time: min/s, messages: count)")
        if isinstance(qty, bool) or qty is None or isinstance(qty, (list, dict)):
            raise UsageFileError(f"row {i}: quantity must be a number")
        try:
            q = float(str(qty).strip().replace(",", ".")) if isinstance(qty, str) else float(qty)   # 2,5 = 2.5
        except ValueError as e:
            raise UsageFileError(f"row {i}: quantity '{qty}' is not a number") from e
        if not (0 <= q <= 1e7):
            raise UsageFileError(f"row {i}: quantity out of range")
        zone = (zone or "").strip().upper() or None
        if ut in (UsageType.ROAMING_DATA, UsageType.ROAMING_VOICE):
            if zone not in ("EU", "Z2", "Z3"):
                raise UsageFileError(f"row {i}: roaming_zone must be EU, Z2 or Z3")
            out[f"{ut}:{zone}"] += int(q * factor)
        else:
            if zone:
                raise UsageFileError(f"row {i}: roaming_zone only applies to ROAMING_DATA / ROAMING_VOICE")
            out[str(ut)] += int(q * factor)
    return dict(out)


MAX_BYTES = 1_000_000


def parse_upload(filename: str, content: bytes) -> dict[str, int]:
    """Every bad file ends in a UsageFileError with a message a customer can act on (never a 500)."""
    if len(content) > MAX_BYTES:
        raise UsageFileError("file too large (max 1 MB)")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:   # Excel's "Unicode text" export
            text = content.decode("utf-16")
        except UnicodeDecodeError as e:
            raise UsageFileError("couldn't read the file: please save it as CSV (UTF-8) or JSON") from e
    if filename.lower().endswith(".json"):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, RecursionError) as e:
            raise UsageFileError("the JSON file isn't valid") from e
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise UsageFileError('JSON must be a list of rows like {"usage_type": "DATA", "quantity": 2, "unit": "GB"}')
        try:
            rows = [(r["usage_type"], r["quantity"], r["unit"], r.get("roaming_zone")) for r in data]
        except KeyError as e:
            raise UsageFileError(f"every row needs usage_type, quantity and unit (missing {e})") from e
    else:
        sample = text[:4096]
        delim = ";" if sample.count(";") > sample.count(",") else ","   # German/Polish Excel saves with ';'
        reader = csv.DictReader(io.StringIO(text), delimiter=delim)
        if reader.fieldnames:
            reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]
        need = {"usage_type", "quantity", "unit"}
        if not reader.fieldnames or not need <= set(reader.fieldnames):
            raise UsageFileError("the CSV header must contain usage_type, quantity, unit (and optionally roaming_zone)")
        rows = [(r.get("usage_type") or "", r.get("quantity"), r.get("unit"), r.get("roaming_zone")) for r in reader]
    if not rows:
        raise UsageFileError("the file contains no usage rows")
    return _counters(rows)


def scenario_counters(key: str) -> dict[str, int]:
    if key not in SCENARIOS:
        raise UsageFileError(f"unknown scenario {key!r}")
    return _counters(SCENARIOS[key]["rows"])


def apply_scenario(key: str, base: dict[str, int]) -> dict[str, int]:
    """The scenario on top of (mode add) or in place of part of (mode replace) the customer's real month."""
    extra = scenario_counters(key)
    out = dict(base)
    if SCENARIOS[key].get("mode") == "replace":
        for k in extra:
            out.pop(k, None)
    for k, v in extra.items():
        out[k] = out.get(k, 0) + v
    return out
