"""Bill-day preparation, run right after the bill run (explain-batch).

On bill day every customer gets a new bill at once. Instead of letting the first view of each bill
trigger database reads and an LLM call (a stampede on both), this job walks the newly issued bills and
  1. warms the home-screen read model in Valkey (bill-day traffic is served from cache), and
  2. generates the AI explanation at a FIXED rate the gateway/providers can sustain (--rps), storing it
     in the same cache the API reads. A customer opening the app then gets it instantly.
Restartable: already-explained bills are cache hits and cost nothing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time

from sqlalchemy import select


def prepare(engine, r, cycle: str, rps: float, explain: bool = True, limit: int | None = None) -> dict:
    from ..agent.explain_llm import explain_bill
    from ..agent.factory import build_llm
    from ..db import bills, subscribers
    from .bills import previous_cycle, row_to_bill
    from .home_cache import warm

    with engine.connect() as cx:
        rows = cx.execute(select(bills, subscribers.c.country).join(subscribers, subscribers.c.id == bills.c.subscriber_id)
                          .where(bills.c.cycle == cycle).order_by(bills.c.id)).all()
        prev = {p.subscriber_id: p for p in cx.execute(select(bills).where(bills.c.cycle == previous_cycle(cycle))).all()}
    stats = {"bills": len(rows), "warmed": 0, "explained": 0, "cached": 0, "failed": 0}
    gap = 1.0 / rps if rps > 0 else 0.0
    for row in rows[:limit]:
        warm(r, engine, row.subscriber_id)
        stats["warmed"] += 1
        if not explain:
            continue
        t0 = time.monotonic()
        try:
            llm = build_llm(row.country, row.subscriber_id)
            llm.meta = {"trace_name": "bill-explanation-batch", "cycle": cycle,
                        "trace_user_id": hashlib.sha256(row.subscriber_id.encode()).hexdigest()[:16]}
            p = prev.get(row.subscriber_id)
            res = explain_bill(llm, r, row_to_bill(row), row_to_bill(p) if p else None, "en")
            hit = "hit" in str((res.get("trace") or {}).get("cache", ""))
            stats["cached" if hit else "explained"] += 1
            if hit:
                continue  # no LLM call made: don't wait
        except Exception:  # noqa: BLE001 - the API falls back to on-demand generation for this bill
            stats["failed"] += 1
        time.sleep(max(0.0, gap - (time.monotonic() - t0)))
    return stats


def main() -> None:
    from datetime import UTC, datetime

    from ..db import get_engine
    from ..deps import get_valkey
    from .bills import cycle_of, previous_cycle

    ap = argparse.ArgumentParser(description="Warm caches and pre-generate bill explanations after the bill run")
    ap.add_argument("--cycle", help="YYYY-MM; default: previous month")
    ap.add_argument("--rps", type=float, default=2.0, help="LLM explanations per second (gateway budget)")
    ap.add_argument("--no-explain", action="store_true", help="only warm the home-screen cache")
    a = ap.parse_args()
    cycle = a.cycle or previous_cycle(cycle_of(datetime.now(UTC)))
    print(json.dumps({"cycle": cycle, **prepare(get_engine(), get_valkey(), cycle, a.rps, not a.no_explain)}))
