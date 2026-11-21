"""LLM access with Layer 3 (prompt caching telemetry) and Layer 4 (budgets, spend caps, routing).

- Every call names a STAGE (grade | generate | repair). Each stage has its own model, max_tokens
  (item-level token budget) and a daily spend cap in USD tracked in Valkey. Over the cap the stage is
  skipped and the agent degrades (e.g. no grader -> RRF threshold), it never overspends.
- Cost is computed from the provider's usage block, including prefix-cache hit/miss tokens
  (DeepSeek bills cache-hit input tokens at a fraction of the miss price). Prices are config.
"""
from __future__ import annotations

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
PRICES = {"deepseek": {"in_hit": 0.07, "in_miss": 0.27, "out": 1.10},
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


class LLM:
    def __init__(self, settings: Settings, valkey, provider: str, http: httpx.Client | None = None):
        self.s, self.r, self.provider = settings, valkey, provider
        self.http = http or httpx.Client(timeout=settings.llm_timeout_s)
        cfg = {"deepseek": ("https://api.deepseek.com/chat/completions", settings.deepseek_api_key, settings.deepseek_model),
               "mistral": ("https://api.mistral.ai/v1/chat/completions", settings.mistral_api_key, settings.mistral_model),
               "selfhosted": (f"{settings.selfhosted_llm_url.rstrip('/')}/v1/chat/completions",
                              settings.selfhosted_llm_key or "none", settings.selfhosted_llm_model)}.get(provider)
        if not cfg or not cfg[1] or (provider == "selfhosted" and not settings.selfhosted_llm_url):
            raise RuntimeError(f"LLM provider {provider!r} is not configured")
        self.url, self.key, self.model = cfg

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
        return body

    def _account(self, stage: str, u: dict, dt: float, text: str, tool_calls: list | None) -> LLMResult:
        hit = int(u.get("prompt_cache_hit_tokens", 0))
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
        r = self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.key}"})
        dt = time.monotonic() - t0
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = [{"id": c["id"], "name": c["function"]["name"], "arguments": c["function"]["arguments"]}
                 for c in msg.get("tool_calls") or []] or None
        return self._account(stage, data.get("usage", {}), dt, (msg.get("content") or "").strip(), calls)
