import httpx
from core_api.db import create_schema
from core_api.services import ai
from core_api.settings import Settings
from sqlalchemy import create_engine
from telco_common.billing import rate
from telco_common.catalog import MB, get_plan
from telco_common.explainer import explain

PLAN = get_plan("DE-FLEX")
EX = explain(rate("s", "2026-10", PLAN, {"DATA": 3_500 * MB, "ROAMING_DATA:Z2": 400 * MB}),
             rate("s", "2026-09", PLAN, {"DATA": 1_000 * MB}))


def narrator(handler, monkeypatch):
    monkeypatch.setattr(ai, "flag_bool", lambda *a, **k: True)
    monkeypatch.setattr(ai, "flag_str", lambda *a, **k: "deepseek")
    monkeypatch.setattr(ai, "_breakers", {"deepseek": ai.CircuitBreaker("t", failures=2, reset_s=60)})
    engine = create_engine("sqlite://")
    create_schema(engine)
    s = Settings(deepseek_api_key="k", database_url="sqlite://")
    return ai.Narrator(s, engine, httpx.Client(transport=httpx.MockTransport(handler)))


def reply(text):
    return lambda req: httpx.Response(200, json={"choices": [{"message": {"content": text}}],
                                                 "usage": {"prompt_tokens": 100, "completion_tokens": 50}})


def test_grounded_answer_is_used_and_cached(monkeypatch):
    total = EX.facts[0].split("from ")[1].split(" EUR")[0]
    calls = []

    def handler(req):
        calls.append(req)
        return reply(f"Your bill went up, mainly due to roaming. Last month it was {total} EUR.")(req)

    n = narrator(handler, monkeypatch)
    text, provider = n.narrate(EX, "s", "DE")
    assert provider == "deepseek:deepseek-chat" and "roaming" in text
    assert str(calls[0].url) == "https://api.deepseek.com/chat/completions"
    n.narrate(EX, "s", "DE")
    assert len(calls) == 1  # second call served from cache
    assert b"4915" not in calls[0].content  # no subscriber identifiers sent to the LLM


def test_hallucinated_number_falls_back_to_template(monkeypatch):
    n = narrator(reply("Your bill rose by 999.99 EUR because of streaming."), monkeypatch)
    text, provider = n.narrate(EX, "s", "DE")
    assert provider == "template" and text == " ".join(EX.facts)


def test_outage_opens_breaker_then_fails_fast(monkeypatch):
    calls = []

    def boom(req):
        calls.append(1)
        return httpx.Response(503)

    n = narrator(boom, monkeypatch)
    for _ in range(4):
        assert n.narrate(EX, "s", "DE")[1] == "template"
    assert len(calls) == 2  # breaker opened after 2 failures; later calls never hit the provider


def test_unconfigured_provider_uses_template(monkeypatch):
    n = narrator(reply("unused"), monkeypatch)
    monkeypatch.setattr(ai, "flag_str", lambda *a, **k: "mistral")  # flag points at a provider with no key
    assert n.narrate(EX, "s", "DE")[1] == "template"


def test_cache_hits_do_not_consume_the_ai_budget(monkeypatch):
    total = EX.facts[0].split("from ")[1].split(" EUR")[0]
    n = narrator(reply(f"It went up because of roaming. Before it was {total} EUR."), monkeypatch)
    spent = []
    budget = lambda: spent.append(1) or len(spent) <= 1
    assert n.narrate(EX, "s", "DE", budget=budget)[1].startswith("deepseek")
    for _ in range(5):
        assert n.narrate(EX, "s", "DE", budget=budget)[1].startswith("deepseek")  # cached: free
    assert len(spent) == 1
