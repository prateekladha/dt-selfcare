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

SCENARIOS: dict[str, dict] = {
    "us-trip-2-weeks": {"title": "2 weeks in the US", "rows": [
        ("DATA", 4, "GB", None), ("ROAMING_DATA", 1.5, "GB", "Z2"), ("ROAMING_VOICE", 90, "min", "Z2"),
        ("VOICE", 200, "min", None), ("SMS", 20, "count", None)]},
    "eu-summer-holiday": {"title": "3 weeks in Spain (EU)", "rows": [
        ("DATA", 3, "GB", None), ("ROAMING_DATA", 6, "GB", "EU"), ("ROAMING_VOICE", 150, "min", "EU"),
        ("VOICE", 100, "min", None)]},
    "heavy-streaming": {"title": "Heavy streaming month", "rows": [
        ("DATA", 45, "GB", None), ("VOICE", 300, "min", None), ("SMS", 15, "count", None)]},
    "calling-family-abroad": {"title": "Calling family abroad", "rows": [
        ("DATA", 5, "GB", None), ("INTL_VOICE", 400, "min", None), ("VOICE", 250, "min", None)]},
    "light-month": {"title": "Light month", "rows": [("DATA", 1, "GB", None), ("VOICE", 60, "min", None)]},
}


class UsageFileError(ValueError):
    pass


def _counters(rows: list[tuple[str, float, str, str | None]]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    if len(rows) > MAX_ROWS:
        raise UsageFileError(f"too many rows (max {MAX_ROWS})")
    for i, (t, qty, unit, zone) in enumerate(rows, start=1):
        try:
            ut = UsageType(str(t).strip().upper())
        except ValueError as e:
            raise UsageFileError(f"row {i}: unknown usage_type {t!r}") from e
        factor = UNIT_FACTORS.get((KIND[ut], str(unit).strip().lower()))
        if factor is None:
            raise UsageFileError(f"row {i}: unit {unit!r} not valid for {ut}")
        q = float(qty)
        if q < 0 or q > 1e7:
            raise UsageFileError(f"row {i}: quantity out of range")
        zone = (zone or "").strip().upper() or None
        if ut in (UsageType.ROAMING_DATA, UsageType.ROAMING_VOICE):
            if zone not in ("EU", "Z2", "Z3"):
                raise UsageFileError(f"row {i}: roaming_zone must be EU, Z2 or Z3")
            out[f"{ut}:{zone}"] += int(q * factor)
        else:
            out[str(ut)] += int(q * factor)
    return dict(out)


def parse_upload(filename: str, content: bytes) -> dict[str, int]:
    if len(content) > 1_000_000:
        raise UsageFileError("file too large (max 1 MB)")
    text = content.decode("utf-8-sig", errors="strict")
    if filename.lower().endswith(".json"):
        data = json.loads(text)
        rows = [(r["usage_type"], r["quantity"], r["unit"], r.get("roaming_zone")) for r in data]
    else:
        reader = csv.DictReader(io.StringIO(text))
        need = {"usage_type", "quantity", "unit"}
        if not reader.fieldnames or not need <= set(reader.fieldnames):
            raise UsageFileError("CSV header must contain usage_type,quantity,unit[,roaming_zone]")
        rows = [(r["usage_type"], r["quantity"], r["unit"], r.get("roaming_zone")) for r in reader]
    if not rows:
        raise UsageFileError("file contains no usage rows")
    return _counters(rows)


def scenario_counters(key: str) -> dict[str, int]:
    if key not in SCENARIOS:
        raise UsageFileError(f"unknown scenario {key!r}")
    return _counters(SCENARIOS[key]["rows"])
