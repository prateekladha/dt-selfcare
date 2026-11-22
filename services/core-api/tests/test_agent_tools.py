"""Assistant: multi-step tool calling through the broker, streamed; structured single-call explanation."""
import asyncio
import json
from datetime import UTC, datetime

import fakeredis
import httpx
import pytest
from core_api.agent.broker import Broker
from core_api.agent.embed import HashEmbedder
from core_api.agent.explain_llm import explain_bill
from core_api.agent.llm import LLM
from core_api.agent.loop import run_stream
from core_api.agent.tools import ToolContext
from core_api.db import create_schema, subscribers
from core_api.services.bills import close_cycle, get_bill_row, row_to_bill
from core_api.settings import Settings
from sqlalchemy import create_engine
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
    for off, (ts, items) in enumerate([(datetime(2026, 9, 10, tzinfo=UTC), [(UsageType.DATA, None, 1_000 * MB)]),
                                       (datetime(2026, 10, 10, tzinfo=UTC), [(UsageType.ROAMING_DATA, "Z2", 372 * MB)])]):
        state.apply(p, off, f"m{off}", [UsageRecord(record_id=f"r-{off}-{i:05d}", subscriber_id=SUB, country=Country.DE,
                                                    usage_type=t, roaming_zone=z, quantity=q, event_time=ts)
                                        for i, (t, z, q) in enumerate(items)], [50, 80, 100])
    close_cycle(engine, state, "2026-09")
    close_cycle(engine, state, "2026-10")
    return engine, r, state


def sse(chunks):
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


USAGE = {"usage": {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200}}


def scripted_llm(r, turns):
    """turns: list of ('tools', [(name, args)]) | ('text', str) | ('json', obj) - consumed in order."""
    def handler(req):
        body = json.loads(req.content)
        kind, val = turns.pop(0)
        if body.get("stream"):
            if kind == "tools":
                calls = [{"index": i, "id": f"call_{i}", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(val)]
                return sse([{"choices": [{"delta": {"tool_calls": calls}}]}, USAGE])
            words = val.split(" ")
            return sse([{"choices": [{"delta": {"content": w + (" " if i < len(words) - 1 else "")}}]} for i, w in enumerate(words)] + [USAGE])
        content = json.dumps(val) if kind == "json" else val
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}], **USAGE})
    s = Settings(deepseek_api_key="k", database_url="sqlite://")
    t = httpx.MockTransport(handler)
    return LLM(s, r, "deepseek", http=httpx.Client(transport=t), ahttp=httpx.AsyncClient(transport=t))


def collect(gen):
    async def go():
        return [e async for e in gen]
    return asyncio.run(go())


def test_multi_step_tool_calling_streams_steps_tokens_and_verifies(world):
    engine, r, state = world
    llm = scripted_llm(r, [
        ("tools", [("list_bills", {"months": 3})]),
        ("tools", [("compare_bills", {"cycle": "2026-10"}), ("get_bill", {"cycle": "2026-10"})]),
        ("text", "Your October bill went up because of roaming in zone Z2: 372 MB [S2][S3]."),
    ])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("Why did my October bill go up?", ctx, llm, engine))
    kinds = [e["type"] for e in events]
    assert kinds.count("step") == 3 and "token" in kinds and kinds[-1] == "done"
    done = events[-1]
    assert done["status"] == "VERIFIED" and {c["tool"] for c in done["citations"]} >= {"compare_bills"}
    assert done["trace"]["totals"]["tool_calls"] == 3 and done["trace"]["totals"]["cache_hit_ratio"] == 0.8
    assert [t["decision"] for t in done["trace"]["tool_calls"]] == ["EXECUTE"] * 3


def test_broker_denies_unknown_tools_and_bad_arguments(world):
    engine, _r, state = world
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder())
    b = Broker(["get_bill"])
    assert b.request("drop_tables", "{}", ctx).decision == "DENY"
    assert b.request("get_bill", '{"cycle": "October"}', ctx).decision == "DENY"  # schema: YYYY-MM
    ok = b.request("get_bill", '{"cycle": "2026-10"}', ctx)
    assert ok.decision == "EXECUTE" and ok.label == "S1" and "372 MB" in b.sources["S1"]


def test_unsupported_number_in_answer_is_repaired(world):
    engine, r, state = world
    llm = scripted_llm(r, [("tools", [("get_bill", {"cycle": "2026-10"})]),
                           ("text", "It rose by 999 EUR [S1]."),
                           ("text", "Roaming in zone Z2 added 372 MB [S1].")])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("Why is it higher?", ctx, llm, engine))
    assert "repairing" in [e["type"] for e in events] and events[-1]["status"] == "REPAIRED"


def test_structured_explanation_single_call_verified_and_cached(world):
    engine, r, _state = world
    cur, prev = row_to_bill(get_bill_row(engine, SUB, "2026-10")), row_to_bill(get_bill_row(engine, SUB, "2026-09"))
    body = {"summary": "Your bill went up because of roaming in zone Z2.",
            "drivers": [{"title": "Roaming data, zone Z2", "impact": "+7.40 EUR", "why": "372 MB outside the EU.", "sources": ["S1", "S4"]}],
            "top_usage": [{"title": "Roaming data, zone Z2", "amount": "7.40 EUR", "why": "372 MB.", "sources": ["S2"]}],
            "tip": "A roaming pack can help.", "tip_sources": ["G2"]}
    llm = scripted_llm(r, [("json", body)])
    out = explain_bill(llm, r, cur, prev)
    assert out["status"] == "VERIFIED" and out["explanation"]["drivers"][0]["impact"] == "+7.40 EUR"
    again = explain_bill(scripted_llm(r, []), r, cur, prev)  # no LLM call available -> must come from cache
    assert again["trace"]["cache"].startswith("hit")


def test_one_customer_cannot_use_up_the_shared_ai_budget(world):
    from core_api.agent import explain_llm

    engine, r, _state = world
    cur, prev = row_to_bill(get_bill_row(engine, SUB, "2026-10")), row_to_bill(get_bill_row(engine, SUB, "2026-09"))
    r.set(f"rl:ai:{SUB}", explain_llm.AI_CALLS_PER_SUBSCRIBER_DAY)  # this customer's fresh explanations for today
    out = explain_bill(scripted_llm(r, []), r, cur, prev)  # no LLM call may happen
    assert out["status"] == "BUDGET" and out["explanation"] is None



def test_greeting_is_a_single_conversational_answer_without_repairs(world):
    engine, r, state = world
    llm = scripted_llm(r, [("text", "Hi! I can help with your bills, usage, plan and payments. What would you like to know?")])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("Hello", ctx, llm, engine))
    kinds = [e["type"] for e in events]
    assert "repairing" not in kinds and "replace" not in kinds and kinds.count("done") == 1
    assert events[-1]["status"] == "CONVERSATIONAL" and events[-1]["answer"].startswith("Hi!")


def test_session_memory_feeds_previous_turns_into_the_next_question(world):
    from core_api.agent.loop import load_history, save_turn
    engine, r, state = world
    save_turn(r, SUB, "sess-0001", "Why did my October bill go up?", "Roaming in zone Z2 [S1].")
    history = load_history(r, SUB, "sess-0001")
    assert history == [{"q": "Why did my October bill go up?", "a": "Roaming in zone Z2 ."}]
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return sse([{"choices": [{"delta": {"content": "It was roaming in zone Z2."}}]}, USAGE])
    t = httpx.MockTransport(handler)
    llm = LLM(Settings(deepseek_api_key="k", database_url="sqlite://"), r, "deepseek",
              http=httpx.Client(transport=t), ahttp=httpx.AsyncClient(transport=t))
    collect(run_stream("And what was it before?", ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm), llm, engine,
                       history=history))
    roles = [m["role"] for m in seen[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"] and "October" in seen[0]["messages"][1]["content"]


def test_chat_history_titles_retention_and_ownership(world):
    import asyncio as _a

    from core_api.agent import history as hist
    engine, r, _state = world
    sid, new = hist.ensure_session(engine, SUB, None)
    assert new and hist.ensure_session(engine, SUB, sid) == (sid, False)
    assert hist.ensure_session(engine, "48600000001", sid)[0] != sid  # another customer can't reuse it
    hist.append_turn(engine, sid, "Why did my October bill go up?", "Roaming in zone Z2 [S1].", "VERIFIED", [{"label": "S1"}])
    assert hist.recent_turns(engine, sid) == [{"q": "Why did my October bill go up?", "a": "Roaming in zone Z2."}]

    good = scripted_llm(r, [("text", "October roaming bill increase")])
    assert _a.run(hist.make_title(good, "Why did my October bill go up?", "Roaming"))[0] == "October roaming bill increase"

    def down(req):
        return httpx.Response(503)
    t = httpx.MockTransport(down)
    bad = LLM(Settings(deepseek_api_key="k", database_url="sqlite://"), r, "deepseek",
              http=httpx.Client(transport=t), ahttp=httpx.AsyncClient(transport=t))
    title, src = _a.run(hist.make_title(bad, "why did my october bill go up so much?", "x"))
    assert (title, src) == ("Why did my october bill go up", "question")  # fallback when the gateway is down

    hist.set_title(engine, sid, "October roaming", "title-llm")
    assert hist.list_sessions(engine, SUB)[0]["title"] == "October roaming"
    assert hist.get_session(engine, "48600000001", sid) is None  # ownership enforced
    for _ in range(hist.MAX_SESSIONS + 3):
        s2, _n = hist.ensure_session(engine, SUB, None)
        hist.append_turn(engine, s2, "q", "a", "CONVERSATIONAL", [])
    assert len(hist.list_sessions(engine, SUB)) <= hist.MAX_SESSIONS  # retention
    assert hist.delete_session(engine, SUB, s2) and hist.get_session(engine, SUB, s2) is None


def test_title_camelcase_is_split(world):
    import asyncio as _a

    from core_api.agent import history as hist
    _engine, r, _state = world
    llm = scripted_llm(r, [("text", "RoamingCostSurprise")])
    assert _a.run(hist.make_title(llm, "Why did my bill go up?", "x"))[0] == "Roaming Cost Surprise"
