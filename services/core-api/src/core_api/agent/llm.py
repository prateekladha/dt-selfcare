"""LLM access with Layer 3 (prompt caching telemetry) and Layer 4 (budgets, spend caps, routing).

- Every call names a STAGE (grade | generate | repair). Each stage has its own model, max_tokens
  (item-level token budget) and a daily spend cap in USD tracked in Valkey. Over the cap the stage is
  skipped and the agent degrades (e.g. no grader -> RRF threshold), it never overspends.
- Cost is computed from the provider's usage block, including prefix-cache hit/miss tokens
  (DeepSeek bills cache-hit input tokens at a fraction of the miss price). Prices are config.
"""
from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from prometheus_client import Counter, Histogram

from ..settings import Settings

LLM_TOKENS = Counter("agent_llm_tokens_total", "LLM tokens", ["stage", "kind"])
LLM_COST = Counter("agent_llm_cost_usd_total", "LLM spend in USD", ["stage"])
LLM_SECONDS = Histogram("agent_llm_seconds", "LLM latency", ["stage"])

# USD per 1M tokens (configurable; defaults = DeepSeek chat list prices at build time)
PRICES = {"gateway": {"in_hit": 0.07, "in_miss": 0.27, "out": 1.10},  # primary model behind the gateway
          "deepseek": {"in_hit": 0.07, "in_miss": 0.27, "out": 1.10},
          "mistral": {"in_hit": 0.10, "in_miss": 0.10, "out": 0.30},
          "selfhosted": {"in_hit": 0.0, "in_miss": 0.0, "out": 0.0}}

STAGES = {  # stage -> (max_tokens, daily cap USD, temperature)
    "grade": (300, 0.50, 0.0),       # cheap stage: CRAG grader
    "generate": (450, 2.00, 0.2),
    "explain": (900, 2.00, 0.2),     # structured bill explanation (single call)
    "agent": (700, 3.00, 0.2),       # tool-calling turns of the assistant
    "repair": (700, 1.00, 0.0),
}


class BudgetExceeded(Exception):
    pass


@dataclass
class LLMResult:
    text: str
    stage: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cache_hit_tokens: int
    cache_miss_tokens: int
    cost_usd: float
    seconds: float
    tool_calls: list | None = None

    def as_trace(self) -> dict:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in self.__dict__.items()
                if k not in ("text", "tool_calls")}


_SYNC_SLOTS: threading.BoundedSemaphore | None = None
_ASYNC_SLOTS: asyncio.Semaphore | None = None
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


def _slots(n: int):
    global _SYNC_SLOTS
    if _SYNC_SLOTS is None:
        _SYNC_SLOTS = threading.BoundedSemaphore(n)
    return _SYNC_SLOTS


def _aslots(n: int):
    global _ASYNC_SLOTS
    if _ASYNC_SLOTS is None:
        _ASYNC_SLOTS = asyncio.Semaphore(n)
    return _ASYNC_SLOTS


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 10.0)
        except ValueError:
            pass
    return min(8.0, 0.5 * 2 ** attempt) * (0.5 + random.random())


class LLM:
    def __init__(self, settings: Settings, valkey, provider: str, http: httpx.Client | None = None,
                 ahttp: httpx.AsyncClient | None = None):
        self.s, self.r, self.provider = settings, valkey, provider
        self.http = http or httpx.Client(timeout=settings.llm_timeout_s)
        self.ahttp = ahttp
        cfg = {"gateway": (f"{settings.llm_gateway_url.rstrip('/')}/chat/completions", settings.llm_gateway_key,
                           settings.llm_model_alias),
               "deepseek": ("https://api.deepseek.com/chat/completions", settings.deepseek_api_key, settings.deepseek_model),
               "mistral": ("https://api.mistral.ai/v1/chat/completions", settings.mistral_api_key, settings.mistral_model),
               "selfhosted": (f"{settings.selfhosted_llm_url.rstrip('/')}/v1/chat/completions",
                              settings.selfhosted_llm_key or "none", settings.selfhosted_llm_model)}.get(provider)
        if not cfg or not cfg[1] or (provider == "selfhosted" and not settings.selfhosted_llm_url) or (
                provider == "gateway" and not settings.llm_gateway_url):
            raise RuntimeError(f"LLM provider {provider!r} is not configured")
        self.url, self.key, self.model = cfg
        self.meta: dict = {}  # trace metadata forwarded to the gateway -> Langfuse (trace name, session, user hash)

    def _spend_key(self, stage: str) -> str:
        return f"spend:{stage}:{datetime.now(UTC):%Y%m%d}"

    def spent_today(self, stage: str) -> float:
        return float(self.r.get(self._spend_key(stage)) or 0)

    def _body(self, stage: str, messages: list[dict], json_mode: bool, tools: list | None, stream: bool) -> dict:
        max_tokens, cap, temp = STAGES[stage]
        if self.spent_today(stage) >= cap:
            raise BudgetExceeded(f"daily cap for stage {stage!r} reached (${cap})")
        body = {"model": self.model, "messages": messages, "max_tokens": max_tokens, "temperature": temp, "stream": stream}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        if self.provider == "gateway" and self.meta:
            body["metadata"] = {**self.meta, "generation_name": stage, "tags": [stage]}
        return body

    def _account(self, stage: str, u: dict, dt: float, text: str, tool_calls: list | None) -> LLMResult:
        hit = int(u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        miss = int(u.get("prompt_cache_miss_tokens", u.get("prompt_tokens", 0) - hit))
        out = int(u.get("completion_tokens", 0))
        price = PRICES.get(self.provider, PRICES["deepseek"])
        cost = (hit * price["in_hit"] + miss * price["in_miss"] + out * price["out"]) / 1_000_000
        k = self._spend_key(stage)
        self.r.incrbyfloat(k, cost)
        self.r.expire(k, 3 * 86400)
        LLM_COST.labels(stage).inc(cost)
        for kind, v in (("cache_hit", hit), ("cache_miss", miss), ("out", out)):
            LLM_TOKENS.labels(stage, kind).inc(v)
        LLM_SECONDS.labels(stage).observe(dt)
        return LLMResult(text, stage, self.model, int(u.get("prompt_tokens", hit + miss)), out, hit, miss, cost, dt,
                         tool_calls)

    def stream(self, stage: str, messages: list[dict], tools: list | None = None):
        """Yields ("token", str) as content arrives, then ("end", LLMResult) with merged tool calls."""
        import json as _json

        body = self._body(stage, messages, False, tools, True)
        t0 = time.monotonic()
        text, calls, usage = [], {}, {}
        with self.http.stream("POST", self.url, json=body, headers={"Authorization": f"Bearer {self.key}"}) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = _json.loads(data)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                for ch in chunk.get("choices", []):
                    delta = ch.get("delta", {})
                    if delta.get("content"):
                        text.append(delta["content"])
                        yield ("token", delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
        merged = [calls[i] for i in sorted(calls)] or None
        yield ("end", self._account(stage, usage, time.monotonic() - t0, "".join(text), merged))

    def chat(self, stage: str, messages: list[dict], json_mode: bool = False, tools: list | None = None) -> LLMResult:
        body = self._body(stage, messages, json_mode, tools, False)
        t0 = time.monotonic()
        with _slots(self.s.llm_max_concurrency):
            for attempt in range(3):
                r = self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
                if r.status_code not in RETRY_STATUS or attempt == 2:
                    break
                time.sleep(_backoff(attempt, r.headers.get("retry-after")))
        dt = time.monotonic() - t0
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = [{"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                 for c in msg.get("tool_calls") or []] or None
        return self._account(stage, data.get("usage", {}), dt, (msg.get("content") or "").strip(), calls)


    # ---------------- async (used by the streaming assistant) ----------------
    def _client(self) -> httpx.AsyncClient:
        if self.ahttp is None:
            self.ahttp = httpx.AsyncClient(timeout=self.s.llm_timeout_s)
        return self.ahttp

    async def achat(self, stage: str, messages: list[dict], json_mode: bool = False, tools: list | None = None) -> LLMResult:
        body = self._body(stage, messages, json_mode, tools, False)
        t0 = time.monotonic()
        async with _aslots(self.s.llm_max_concurrency):
            for attempt in range(3):
                r = await self._client().post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
                if r.status_code not in RETRY_STATUS or attempt == 2:
                    break
                await asyncio.sleep(_backoff(attempt, r.headers.get("retry-after")))
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = [{"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                 for c in msg.get("tool_calls") or []] or None
        return self._account(stage, data.get("usage", {}), time.monotonic() - t0, (msg.get("content") or "").strip(), calls)

    async def astream(self, stage: str, messages: list[dict], tools: list | None = None):
        """Async SSE stream: yields ("token", str) then ("end", LLMResult). Retries the connect on 429/5xx."""
        import json as _json

        body = self._body(stage, messages, False, tools, True)
        t0 = time.monotonic()
        text, calls, usage = [], {}, {}
        async with _aslots(self.s.llm_max_concurrency):
            for attempt in range(3):
                async with self._client().stream("POST", self.url, json=body,
                                                 headers={"Authorization": f"Bearer {self.key}"}) as r:
                    if r.status_code in RETRY_STATUS and attempt < 2:
                        await asyncio.sleep(_backoff(attempt, r.headers.get("retry-after")))
                        continue
                    r.raise_for_status()
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        chunk = _json.loads(data)
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for ch in chunk.get("choices", []):
                            delta = ch.get("delta", {})
                            if delta.get("content"):
                                text.append(delta["content"])
                                yield ("token", delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                                slot["id"] = tc.get("id") or slot["id"]
                                fn = tc.get("function") or {}
                                slot["name"] += fn.get("name") or ""
                                slot["arguments"] += fn.get("arguments") or ""
                    break
        merged = [calls[i] for i in sorted(calls)] or None
        yield ("end", self._account(stage, usage, time.monotonic() - t0, "".join(text), merged))
