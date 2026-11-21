"""network-emulator: stands in for the operator's mediation layer (the only synthetic part).

  backfill   12 months of history for the demo population (idempotent: deterministic batch ids)
  live       continuous "now" traffic for the demo population (drives alerts + unbilled amount)
  load       high-rate synthetic traffic over a large population, for throughput tests
"""
from __future__ import annotations

import asyncio
import random
import time
from datetime import UTC, date, datetime

import typer
from telco_common.catalog import get_plan
from telco_common.demo import DEMO_SUBSCRIBERS

from .personas import day_records, days_between, synthetic_record
from .sender import Sender

app = typer.Typer(no_args_is_help=True)
BATCH = 2000


def _months_back(today: date, months: int) -> date:
    y, m = today.year, today.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


@app.command()
def backfill(collector: str = typer.Option(..., envvar="COLLECTOR_URL"),
             token: str = typer.Option(..., envvar="SOURCE_TOKEN"),
             source: str = typer.Option("emu-mediation-01", envvar="SOURCE_ID"),
             months: int = 12, until: str = typer.Option("", help="YYYY-MM-DD, default yesterday")) -> None:
    today = datetime.now(UTC).date()
    end = date.fromisoformat(until) if until else date.fromordinal(today.toordinal() - 1)
    start = _months_back(today, months - 1)

    async def go() -> None:
        sender = Sender(collector, source, token)
        tasks = []
        for d in days_between(start, end):
            recs = [r for s in DEMO_SUBSCRIBERS for r in day_records(s.subscriber_id, s.country, s.persona, get_plan(s.plan_code).data_mb, d, today)]
            for i in range(0, len(recs), BATCH):
                tasks.append(sender.send(f"{source}:backfill:{d:%Y%m%d}:{i // BATCH:04d}", recs[i:i + BATCH]))
        await asyncio.gather(*tasks)
        await sender.close()
        typer.echo(f"backfill {start}..{end}: {sender.stats}")

    asyncio.run(go())


@app.command()
def live(collector: str = typer.Option(..., envvar="COLLECTOR_URL"),
         token: str = typer.Option(..., envvar="SOURCE_TOKEN"),
         source: str = typer.Option("emu-mediation-01", envvar="SOURCE_ID"),
         interval_s: int = typer.Option(60, help="one batch per interval"),
         day_fraction: float = typer.Option(0.05, help="share of a persona's daily usage per interval")) -> None:
    """Emits today's usage gradually: every interval a slice of each persona's day."""

    async def go() -> None:
        sender = Sender(collector, source, token)
        seq = 0
        while True:
            now = datetime.now(UTC)
            recs = []
            for s in DEMO_SUBSCRIBERS:
                day = day_records(s.subscriber_id, s.country, s.persona, get_plan(s.plan_code).data_mb,
                                  now.date(), now.date())
                rng = random.Random(f"{s.subscriber_id}|{now.isoformat()}")
                for r in day:
                    if r.get("kind") == "PLAN_CHANGED" or rng.random() > day_fraction:
                        continue
                    r = r | {"record_id": f"{r['record_id']}-L{now:%H%M%S}-{seq}", "event_time": now.isoformat()}
                    recs.append(r)
                    seq += 1
            if recs:
                await sender.send(f"{source}:live:{now:%Y%m%d%H%M%S}", recs)
                typer.echo(f"{now:%H:%M:%S} sent {len(recs)} records {sender.stats}")
            await asyncio.sleep(interval_s)

    asyncio.run(go())


@app.command()
def load(collector: str = typer.Option(..., envvar="COLLECTOR_URL"),
         token: str = typer.Option(..., envvar="SOURCE_TOKEN"),
         source: str = typer.Option("emu-loadgen-01", envvar="SOURCE_ID"),
         rate: int = typer.Option(5000, help="target records/second"),
         subscribers: int = 1_000_000, duration_s: int = 120, concurrency: int = 16) -> None:
    async def go() -> None:
        sender = Sender(collector, source, token, concurrency=concurrency)
        rng = random.Random(42)
        t0, seq, pending = time.monotonic(), 0, set()
        while time.monotonic() - t0 < duration_s:
            tick = time.monotonic()
            now = datetime.now(UTC)
            per_tick = max(1, rate // 4)  # 4 ticks per second
            for start in range(0, per_tick, BATCH):
                n = min(BATCH, per_tick - start)
                recs = [synthetic_record(rng, subscribers, seq + k, now) for k in range(n)]
                seq += n
                pending.add(asyncio.create_task(sender.send(f"{source}:{now:%Y%m%d%H%M%S%f}:{start}", recs)))
            pending = {t for t in pending if not t.done()}
            await asyncio.sleep(max(0.0, 0.25 - (time.monotonic() - tick)))
        await asyncio.gather(*pending)
        elapsed = time.monotonic() - t0
        await sender.close()
        typer.echo(f"sent {sender.stats['records']:,} records in {elapsed:.1f}s = "
                   f"{sender.stats['records'] / elapsed:,.0f} rec/s; {sender.stats}")

    asyncio.run(go())


if __name__ == "__main__":
    app()
