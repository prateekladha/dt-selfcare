"""Feature flags through OpenFeature (vendor-neutral API) backed by flagd in the cluster.

Every evaluation carries the subscriber as targeting key and the country as an attribute,
so flags can be rolled out per country / percentage / cohort without redeploys.
Defaults here are the *safe* values used if flagd is unreachable (fail-safe, not fail-open).
"""
from __future__ import annotations

import os
from typing import Any

from openfeature import api
from openfeature.evaluation_context import EvaluationContext

# key -> safe default
DEFAULTS: dict[str, Any] = {
    "ai-explanations": False,  # off unless flagd says on: AI is an enhancement, never a dependency
    "ai-provider": "deepseek",  # deepseek | mistral | selfhosted | template
    "payments-enabled": True,
    "payment-gateway": "razorpay",
    "payment-settlement": {"currency": "native"},
    "usage-alert-thresholds": {"levels": [50, 80, 100]},
    "throttle-at-100": True,
    "demo-accounts-panel": False,  # safe default: never expose account summaries pre-login
}

_initialised = False


def init_flags() -> None:
    """Wire flagd if FLAGD_HOST is set; otherwise the no-op provider returns DEFAULTS."""
    global _initialised
    if _initialised:
        return
    host = os.getenv("FLAGD_HOST")
    if host:
        from openfeature.contrib.provider.flagd import FlagdProvider

        api.set_provider(FlagdProvider(host=host, port=int(os.getenv("FLAGD_PORT", "8013"))))
    _initialised = True


def _ctx(country: str | None, subscriber_id: str | None) -> EvaluationContext:
    attrs = {"country": country} if country else {}
    return EvaluationContext(targeting_key=subscriber_id, attributes=attrs)


def flag_bool(key: str, country: str | None = None, subscriber_id: str | None = None) -> bool:
    return api.get_client().get_boolean_value(key, bool(DEFAULTS[key]), _ctx(country, subscriber_id))


def flag_str(key: str, country: str | None = None, subscriber_id: str | None = None) -> str:
    return api.get_client().get_string_value(key, str(DEFAULTS[key]), _ctx(country, subscriber_id))


def flag_obj(key: str, country: str | None = None, subscriber_id: str | None = None) -> dict:
    return api.get_client().get_object_value(key, DEFAULTS[key], _ctx(country, subscriber_id))
