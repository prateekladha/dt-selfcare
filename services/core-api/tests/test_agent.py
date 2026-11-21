"""Agent: L1 spine from real records, L2 hybrid retrieval + grader + CRAG, verify/repair, L3 cache, L4 caps."""
import json
from datetime import UTC, datetime

import fakeredis
import httpx
import pytest
from core_api.agent.agent import BillingAgent, verify
from core_api.agent.embed import HashEmbedder
from core_api.agent.llm import LLM
from core_api.agent.retrieve import hybrid, rrf
from core_api.agent.spine import refresh_global, refresh_subscriber
from core_api.db import agent_traces, context_docs, create_schema, subscribers
from core_api.services.bills import close_cycle
from core_api.settings import Settings
from sqlalchemy import create_engine, func, select
from sqlalchemy.pool import StaticPool
from telco_common.catalog import MB
from telco_common.models import Country, UsageRecord, UsageType
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState

SUB = "4915100000002"


@pytest.fixture
def world():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_schema(engine)
    r = fakeredis.FakeRedis()
    state = UsageState(r, 4)
    with engine.begin() as cx:
        cx.execute(subscribers.insert().values(id=SUB, name="X", email="x@example.com", country="DE",
                                               plan_code="DE-FLEX", password_hash="-"))
    state.upsert_profile(SUB, "DE-FLEX")
    p = partition_for(SUB, 4)

    def usage(ts, items, off):
        recs = [UsageRecord(record_id=f"r-{off}-{i:04d}", subscriber_id=SUB, country=Country.DE, usage_type=t,
                            roaming_zone=z, quantity=q, event_time=ts) for i, (t, z, q) in enumerate(items)]
        state.apply(p, off, f"m{off}", recs, [50, 80, 100])

    usage(datetime(2026, 9, 10, tzinfo=UTC), [(UsageType.DATA, None, 1_000 * MB)], 1)
    usage(datetime(2026, 10, 10, tzinfo=UTC), [(UsageType.DATA, None, 1_500 * MB), (UsageType.ROAMING_DATA, "Z2", 372 * MB),
                                               (UsageType.ROAMING_VOICE, "Z2", 700)], 2)
    close_cycle(engine, state, "2026-09")
    close_cycle(engine, state, "2026-10")
    return engine, r, state


def fake_llm(r, replies, calls):
    def handler(req):
        body = json.loads(req.content)
        calls.append(body)
        if body.get("response_format"):
            content = json.dumps({"relevant": ["D1", "D2", "D3", "D4"], "sufficient": True, "missing": ""})
        else:
            content = replies.pop(0)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {
            "prompt_tokens": 1200, "completion_tokens": 90, "prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 300}})
    s = Settings(deepseek_api_key="k", database_url="sqlite://")
    return s, LLM(s, r, "deepseek", http=httpx.Client(transport=httpx.MockTransport(handler)))


def agent(world, replies, calls):
    engine, r, state = world
    s, llm = fake_llm(r, replies, calls)
    return BillingAgent(s, engine, r, state, HashEmbedder(), llm)


def test_spine_is_built_from_records_and_reembeds_only_changes(world):
    engine, _r, state = world
    first = refresh_subscriber(engine, state, HashEmbedder(), SUB)
    assert first["embedded"] == first["docs"] >= 6  # profile, 2 bills, 2 changes, live usage
    assert refresh_subscriber(engine, state, HashEmbedder(), SUB)["embedded"] == 0  # nothing changed
    refresh_global(engine, HashEmbedder())
    with engine.connect() as cx:
        body = cx.execute(select(context_docs.c.body).where(context_docs.c.id == f"{SUB}:bill:2026-10")).scalar_one()
        refs = [x for (x,) in cx.execute(select(context_docs.c.source_ref))]
    assert "372 MB" in body and "X" not in body.split()  # facts from records, no customer name
    assert any(s.startswith("valkey:u:") for s in refs) and any(s.startswith("adb:bills:") for s in refs)


def test_hybrid_retrieval_puts_the_right_cycle_first(world):
    engine, _r, state = world
    refresh_subscriber(engine, state, HashEmbedder(), SUB)
    docs, meta = hybrid(engine, HashEmbedder(), "Why did my October bill go up?", SUB)
    assert meta["target_cycle"] == "2026-10" and set(meta["rankers"]) == {"bm25", "vector", "cycle"}
    assert docs[0].cycle == "2026-10" and docs[0].scores["rrf"] > 0
    assert rrf({"a": ["x", "y"], "b": ["y", "x"]})["x"] == pytest.approx(rrf({"a": ["x", "y"], "b": ["y", "x"]})["y"])


def test_verified_answer_with_citations_cost_and_trace(world):
    calls = []
    a = agent(world, ["Your October bill rose because of roaming in zone Z2: 372 MB [D1][D2][D3][D4]."], calls)
    res = a.ask("Why did my October bill go up?", SUB, cycle="2026-10")
    assert res.status == "VERIFIED" and res.citations[0]["source_ref"].startswith(("adb:bills", "explainer:"))
    t = res.trace
    assert t["totals"]["llm_calls"] == 2 and t["totals"]["cache_hit_ratio"] == 0.75 and t["totals"]["cost_usd"] > 0
    assert t["retrieval"]["results"][0]["scores"]["rrf"] > 0
    assert calls[1]["messages"][0]["content"].startswith("You are the billing assistant")  # invariant prefix
    assert "4915100000002" not in calls[1]["messages"][1]["content"]  # no identifiers sent to the LLM
    with world[0].connect() as cx:
        assert cx.execute(select(func.count()).select_from(agent_traces)).scalar_one() == 1


def test_unsupported_number_is_repaired_not_templated(world):
    calls = []
    a = agent(world, ["Your bill rose by 999 EUR [D1].", "Roaming in zone Z2 added 372 MB [D1][D2][D3][D4]."], calls)
    res = a.ask("Why did my October bill go up?", SUB, cycle="2026-10")
    assert res.status == "REPAIRED" and "999" not in res.answer
    assert "not in the cited documents" in calls[-1]["messages"][-1]["content"]


def test_never_verified_says_so_honestly(world):
    a = agent(world, ["It went up by 999 EUR [D1].", "Still 999 EUR [D1].", "Again 999 EUR [D1]."], [])
    res = a.ask("Why did my October bill go up?", SUB, cycle="2026-10")
    assert res.status == "UNVERIFIED" and "couldn't produce an explanation" in res.answer


def test_answer_cache_and_stage_spend_cap(world):
    _engine, r, _state = world
    calls = []
    a = agent(world, ["Roaming in zone Z2 added 372 MB [D1][D2][D3][D4]."], calls)
    a.ask("Why did my October bill go up?", SUB, cycle="2026-10")
    n = len(calls)
    again = a.ask("Why did my October bill go up?", SUB, cycle="2026-10")
    assert again.trace["cache"].startswith("hit") and len(calls) == n + 1  # only the grader ran again
    r.set(f"spend:generate:{datetime.now(UTC):%Y%m%d}", 99)
    capped = agent(world, [], []).ask("What is my data usage this month?", SUB)
    assert capped.status == "BUDGET"


def test_verify_rules():
    labels = {"D1": "Roaming data 372 MB costs 7.40 EUR"}
    assert verify("Roaming cost 7.40 EUR [D1].", labels) == []
    assert verify("Roaming cost 7.40 EUR.", labels) == ["no citations"]
    assert any("[D9]" in p for p in verify("x [D9].", labels))

