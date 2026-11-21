from __future__ import annotations

from telco_common.flags import flag_str

from ..db import get_engine
from ..deps import get_usage_state, get_valkey
from ..settings import get_settings
from .agent import BillingAgent
from .embed import get_embedder
from .llm import LLM


def build_agent(country: str | None, subscriber_id: str) -> BillingAgent:
    s = get_settings()
    provider = flag_str("ai-provider", country, subscriber_id)
    if provider == "template":
        provider = "deepseek"
    return BillingAgent(s, get_engine(), get_valkey(), get_usage_state(), get_embedder(), LLM(s, get_valkey(), provider))


def build_llm(country: str | None, subscriber_id: str) -> LLM:
    provider = flag_str("ai-provider", country, subscriber_id)
    return LLM(get_settings(), get_valkey(), "deepseek" if provider == "template" else provider)
