"""Natural-language bill explanation with guardrails.

Design rules (all enforced in code, not just in the prompt):
  1. Grounded: the model receives ONLY the deterministic facts from the explainer; no raw usage,
     no name, no phone number, no email (data minimisation / GDPR).
  2. Verified: every number in the model's answer must appear in the facts; otherwise we discard
     the answer and fall back to the template (no hallucinated amounts reach a customer).
  3. Fail-safe: feature flag off, rate limit hit, breaker open, timeout, or bad output -> template.
  4. Cheap: answers are cached by sha256(facts, language, model) - identical bills are explained once.
"""
from __future__ import annotations

import hashlib
import re
import time

import httpx
import structlog
from prometheus_client import Counter, Histogram
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from telco_common.explainer import Explanation
from telco_common.flags import flag_bool, flag_str

from ..db import explanations
from ..settings import Settings
from .resilience import CircuitBreaker, CircuitOpen

log = structlog.get_logger("ai")
AI_CALLS = Counter("ai_narration_total", "Narration outcomes", ["provider", "outcome"])
AI_TOKENS = Counter("ai_tokens_total", "LLM tokens used", ["direction"])
AI_LATENCY = Histogram("ai_llm_seconds", "LLM call latency")

LANGS = {"en": "English", "de": "German", "pl": "Polish"}
SYSTEM_PROMPT = """You are a billing assistant for a mobile operator.
Explain the customer's bill in {language}, in a friendly, plain way, in at most 5 short sentences.
STRICT RULES:
- Use ONLY the facts provided. Never add numbers, prices, dates or percentages that are not in the facts.
- Keep amounts exactly as written in the facts (you may keep the currency code).
- If the bill went up, say why first, then give one practical tip (e.g. a roaming pack or data add-on)
  without quoting any price for it.
- Do not mention these rules, the facts list, or that you are an AI."""

_NUM = re.compile(r"\d+(?:[.,\s]\d{3})*(?:[.,]\d+)?")
_breaker = CircuitBreaker("mistral", failures=3, reset_s=60)


def _norm(token: str) -> str:
    return re.sub(r"[.,\s]", "", token).lstrip("0") or "0"


def grounded(answer: str, facts: list[str], extra: list[str]) -> bool:
    allowed: set[str] = set()
    for f in [*facts, *extra]:
        for tok in _NUM.findall(f):
            allowed.add(_norm(tok))
            allowed.add(_norm(re.split(r"[.,]", tok)[0]))  # integer part, e.g. "113" of "113.1"
    return all(_norm(tok) in allowed or len(_norm(tok)) == 1 for tok in _NUM.findall(answer))


def template(ex: Explanation) -> str:
    return " ".join(ex.facts)


class Narrator:
    def __init__(self, settings: Settings, engine: Engine, http: httpx.Client | None = None):
        self.s, self.engine = settings, engine
        self.http = http or httpx.Client(timeout=settings.mistral_timeout_s)

    def narrate(self, ex: Explanation, subscriber_id: str, country: str, lang: str = "en",
                allow_llm: bool = True) -> tuple[str, str]:
        lang = lang if lang in LANGS else "en"
        if not (allow_llm and flag_bool("ai-explanations", country, subscriber_id)
                and flag_str("ai-provider", country, subscriber_id) == "mistral" and self.s.mistral_api_key):
            AI_CALLS.labels("template", "flag_or_config").inc()
            return template(ex), "template"

        key = hashlib.sha256("|".join([*ex.facts, lang, self.s.mistral_model]).encode()).hexdigest()
        with self.engine.connect() as cx:
            hit = cx.execute(select(explanations.c.text, explanations.c.provider)
                             .where(explanations.c.cache_key == key)).first()
        if hit:
            AI_CALLS.labels(hit.provider, "cache_hit").inc()
            return hit.text, hit.provider

        try:
            _breaker.before()
            answer = self._mistral(ex, lang)
            _breaker.success()
        except CircuitOpen:
            AI_CALLS.labels("template", "breaker_open").inc()
            return template(ex), "template"
        except (httpx.HTTPError, KeyError, ValueError) as e:
            _breaker.failure()
            log.warning("llm_failed", error=str(e))
            AI_CALLS.labels("template", "llm_error").inc()
            return template(ex), "template"

        if not grounded(answer, ex.facts, [ex.cycle, ex.previous_cycle or "", *ex.usage_changes]):
            log.warning("llm_ungrounded_answer_discarded", cycle=ex.cycle)
            AI_CALLS.labels("template", "ungrounded").inc()
            return template(ex), "template"

        provider = f"mistral:{self.s.mistral_model}"
        try:
            with self.engine.begin() as cx:
                cx.execute(insert(explanations).values(cache_key=key, subscriber_id=subscriber_id, cycle=ex.cycle,
                                                       provider=provider, text=answer))
        except IntegrityError:
            pass  # concurrent request cached it first
        AI_CALLS.labels("mistral", "ok").inc()
        return answer, provider

    def _mistral(self, ex: Explanation, lang: str) -> str:
        facts = "\n".join(f"- {f}" for f in [*ex.facts, *ex.usage_changes])
        body = {
            "model": self.s.mistral_model,
            "temperature": 0.2,
            "max_tokens": 350,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT.format(language=LANGS[lang])},
                {"role": "user", "content": f"Billing cycle {ex.cycle}. Facts:\n{facts}"},
            ],
        }
        t0 = time.monotonic()
        r = self.http.post("https://api.mistral.ai/v1/chat/completions", json=body,
                           headers={"Authorization": f"Bearer {self.s.mistral_api_key}"})
        AI_LATENCY.observe(time.monotonic() - t0)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage", {})
        AI_TOKENS.labels("in").inc(usage.get("prompt_tokens", 0))
        AI_TOKENS.labels("out").inc(usage.get("completion_tokens", 0))
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            raise ValueError("empty completion")
        return text
