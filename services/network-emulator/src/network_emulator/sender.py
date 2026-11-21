"""HTTP client for the collector with retry semantics matching its contract."""
from __future__ import annotations

import asyncio
import random

import httpx


class Sender:
    def __init__(self, url: str, source: str, token: str, concurrency: int = 8):
        self.url = url.rstrip("/") + "/v1/usage-batches"
        self.source = source
        self.client = httpx.AsyncClient(timeout=30, headers={"Authorization": f"Bearer {token}", "X-Source": source})
        self.sem = asyncio.Semaphore(concurrency)
        self.stats = {"accepted": 0, "duplicate": 0, "records": 0, "retries": 0}

    async def send(self, batch_id: str, records: list[dict]) -> None:
        body = {"batch_id": batch_id, "source": self.source, "records": records}
        delay = 0.5
        async with self.sem:
            for _attempt in range(8):
                try:
                    r = await self.client.post(self.url, json=body)
                except httpx.TransportError:
                    r = None
                if r is not None and r.status_code in (200, 202):
                    status = r.json()["status"]
                    self.stats["accepted" if status == "accepted" else "duplicate"] += 1
                    self.stats["records"] += len(records)
                    return
                if r is not None and r.status_code not in (409, 429, 503):
                    raise RuntimeError(f"collector rejected batch {batch_id}: {r.status_code} {r.text[:300]}")
                self.stats["retries"] += 1
                await asyncio.sleep(delay + random.random() * delay)
                delay = min(delay * 2, 15)
            raise RuntimeError(f"giving up on batch {batch_id}")

    async def close(self) -> None:
        await self.client.aclose()
