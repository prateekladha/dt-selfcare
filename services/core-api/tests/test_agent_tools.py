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
            if kind == "text_then_tools":
                text, tools_ = val
                calls = [{"index": i, "id": f"call_{i}", "function": {"name": n, "arguments": json.dumps(a)}} for i, (n, a) in enumerate(tools_)]
                return sse([{"choices": [{"delta": {"content": w + " "}}]} for w in text.split(" ")]
                           + [{"choices": [{"delta": {"tool_calls": calls}}]}, USAGE])
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
    assert kinds.count("step") == 4 and "token" in kinds and kinds[-1] == "done"   # snapshot + 3 tool calls
    done = events[-1]
    assert done["status"] == "VERIFIED" and {c["tool"] for c in done["citations"]} >= {"compare_bills"}
    assert [t["decision"] for t in done["trace"]["tool_calls"]] == ["EXECUTE"] * 7   # 4 snapshot reads + 3 calls


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
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert events[-1]["status"] == "REPAIRED" and "999" not in streamed            # the wrong draft never shown
    assert not {"repairing", "replace"} & {e["type"] for e in events}             # no visible "checking"
    assert streamed == events[-1]["answer"]                                        # only the final answer


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
    # memory, then the question, then the fresh account snapshot (one assistant tool-call turn + 4 results)
    assert roles == ["system", "user", "assistant", "user", "assistant", "tool", "tool", "tool", "tool"]
    assert "October" in seen[0]["messages"][1]["content"] and seen[0]["messages"][4]["tool_calls"][0]["function"]["name"] == "get_current_usage"


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


def test_answer_from_chat_history_is_repaired_by_looking_the_fact_up_not_dropped(world):
    """Prod bug: the model answered "due 15 November 2026" from an earlier answer without calling a tool;
    the old tool-less repair invented [S1], then replied "I don't have the bill details" and that
    replaced the correct answer. Now the repair may call tools, and fact-dropping rewrites are rejected."""
    engine, r, state = world
    d = get_bill_row(engine, SUB, "2026-10").due_date.date()
    due = f"{d.day} {d:%B %Y}"
    llm = scripted_llm(r, [
        ("text", "Your October bill was due on 15 November 2026."),                 # from history, no tools
        ("text", "I don't have the bill details in front of me right now."),       # repair 1: drops the facts
        ("tools", [("get_bill", {"cycle": "2026-10"})]),                            # repair 2: looks it up
        ("text", f"Your October bill is due on {due} [S1]."),
    ])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("What is the due date?", ctx, llm, engine,
                                history=[{"q": "current dues?", "a": "October 41.59 EUR, due 15 November 2026"}]))
    done = events[-1]
    # facts first: the turn's snapshot already holds the bill and its due date, so the answer is checked against
    # fresh data at once (no tool-less draft, no "I don't have the details" rewrite)
    assert done["status"] == "VERIFIED" and "[S" in done["answer"] and "don't have" not in done["answer"]
    kinds = [e["type"] for e in events]
    assert "step" in kinds and "replace" not in kinds and "repairing" not in kinds   # lookup steps shown, no swap
    assert "".join(e["text"] for e in events if e["type"] == "token") == done["answer"]
    assert done["citations"] and done["citations"][0]["tool"] in ("list_bills", "get_bill")   # the bill's due date


def test_bill_tools_return_day_counts_so_the_model_never_does_date_maths(world):
    from datetime import timedelta

    from core_api.agent.tools import _due
    now = datetime.now(UTC)
    week_ago = (now - timedelta(days=7)).date()
    assert _due(now - timedelta(days=7), 41, 0) == {"due_date": f"{week_ago.day} {week_ago:%B %Y}",   # in words
                                                    "payment_status": "overdue", "days_overdue": 7}
    assert _due(now + timedelta(days=3), 41, 0)["days_until_due"] == 3
    paid = _due(now - timedelta(days=7), 41, 41)
    assert paid["payment_status"] == "paid" and "days_overdue" not in paid


def test_failed_answers_are_not_fed_back_as_conversation_context(world):
    from core_api.agent import history as hist
    engine, _r, _state = world
    sid, _ = hist.ensure_session(engine, SUB, None)
    hist.append_turn(engine, sid, "What is the due date?", "I couldn't put together an answer I can verify.", "UNVERIFIED", [])
    hist.append_turn(engine, sid, "Why higher?", "Roaming in zone Z2 [S1].", "VERIFIED", [])
    assert [t["q"] for t in hist.recent_turns(engine, sid)] == ["Why higher?"]


def test_answer_pointing_to_a_checkout_that_was_never_prepared_gets_the_payment_prepared_by_code(world):
    """Prod: 'Please finish the payment in the secure checkout below' with no propose_payment call, so no
    Pay card was shown. Code now prepares the payment itself: no extra LLM round, no rewritten answer."""
    engine, r, state = world
    llm = scripted_llm(r, [
        ("tools", [("list_bills", {"months": 3})]),
        ("text", "Yes, pay the October bill first. Please finish the payment in the secure checkout below."),
    ])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("should I pay the bill first?", ctx, llm, engine))
    kinds = [e["type"] for e in events]
    assert [e["kind"] for e in events if e["type"] == "proposal"] == ["PAYMENT"]
    assert events[-1]["status"] == "VERIFIED" and "repairing" not in kinds and "replace" not in kinds


def test_correct_answer_without_citation_markers_is_not_rewritten_sources_are_attached(world):
    """Prod: 7 of 8 answers were rewritten only because the model left out [S#] markers, so the customer
    saw a different answer after the 'double-check'. Now code attaches the sources; no repair, no replace."""
    engine, r, state = world
    total = row_to_bill(get_bill_row(engine, SUB, "2026-10")).total
    amount = f"{total / 100:.2f} EUR"
    llm = scripted_llm(r, [("tools", [("get_bill", {"cycle": "2026-10"})]),
                           ("text", f"Your October bill comes to {amount}. Most of it is roaming in zone Z2: 372 MB.")])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("Why is it higher?", ctx, llm, engine))
    kinds = [e["type"] for e in events]
    done = events[-1]
    assert done["status"] == "VERIFIED" and "repairing" not in kinds and "replace" not in kinds
    assert done["answer"].count("[S") == 2 and {c["tool"] for c in done["citations"]} <= {"get_bill", "list_bills", "get_current_usage"}


def test_preamble_before_a_tool_call_is_never_streamed_so_nothing_flickers(world):
    engine, r, state = world
    llm = scripted_llm(r, [("text_then_tools", ("Let me check your bills.", [("list_bills", {"months": 3})])),
                           ("text", "Your last three bills are all paid, and nothing is outstanding right now. Anything else?")])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("Any dues?", ctx, llm, engine))
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Let me check" not in streamed and "reset" not in [e["type"] for e in events]
    assert streamed.startswith("Your last three bills")



def test_figures_given_before_any_lookup_are_never_streamed(world):
    """With chat history, the model sometimes answers from earlier answers without tools. Such an answer is
    checked and replaced, so it must not appear on screen first (that was the visible 'answer changes')."""
    engine, r, state = world
    total = row_to_bill(get_bill_row(engine, SUB, "2026-10")).total
    llm = scripted_llm(r, [
        ("text", "Your October bill is 52.37 EUR and it is overdue since 15 November 2026, please pay it soon."),
        ("tools", [("get_bill", {"cycle": "2026-10"})]),
        ("text", f"Your October bill is {total / 100:.2f} EUR."),
    ])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("what do I owe?", ctx, llm, engine, history=[{"q": "dues?", "a": "52.37 EUR"}]))
    streamed = "".join(e["text"] for e in events if e["type"] == "token")
    assert "52.37" not in streamed and events[-1]["status"] == "REPAIRED"
    assert f"{total / 100:.2f} EUR" in events[-1]["answer"]


def test_approval_cards_come_with_the_final_answer_once_never_from_a_discarded_draft(world):
    """A draft prepared the payment, the repair prepared it again: the card used to be sent twice (same key,
    duplicated card) and before the answer was final. Now one card, sent with the final answer."""
    engine, r, state = world
    total = row_to_bill(get_bill_row(engine, SUB, "2026-09")).total
    llm = scripted_llm(r, [
        ("tools", [("propose_payment", {"purpose": "bill"})]),
        ("text", "Your bill is 999 EUR. You can pay it in the secure checkout below."),     # wrong figure
        ("tools", [("propose_payment", {"purpose": "bill"})]),
        ("text", f"Your bill is {total / 100:.2f} EUR. You can pay it in the secure checkout below."),
    ])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("pay my bill", ctx, llm, engine))
    kinds = [e["type"] for e in events]
    assert kinds.count("proposal") == 1
    assert kinds.index("proposal") > max(i for i, k in enumerate(kinds) if k == "step_done")   # after all lookups
    assert events[-1]["status"] == "REPAIRED"


def test_long_tool_results_are_not_cut_for_the_checker():
    """Results were cut at 3,500 characters: figures past the cut were 'not in the tool results'."""
    from core_api.agent.broker import RESULT_CHARS, Broker
    from core_api.agent.loop import check_numbers
    from core_api.agent.tools import TOOLS, Tool
    long = {"source": "x", "rows": [f"row {i}: {1000 + i}.50 EUR" for i in range(150)]}   # ~4k chars
    TOOLS["_long"] = Tool("_long", "test", {"type": "object"}, lambda a, c: long)
    try:
        b = Broker(["_long"])
        b.request("_long", "{}", ToolContext(SUB, "DE", None, None, None))
        assert len(b.sources["S1"]) < RESULT_CHARS and check_numbers("The last one is 1149.50 EUR.", b.sources) == []
    finally:
        del TOOLS["_long"]


def test_reopened_chat_keeps_the_pay_card_its_answer_points_to(world):
    from core_api.agent import history as hist
    engine, _r, _state = world
    sid, _ = hist.ensure_session(engine, SUB, None)
    card = {"type": "proposal", "kind": "PAYMENT", "cycle": "2026-10", "price": "41.59 EUR", "order_id": "pay:BILL:2026-10"}
    hist.append_turn(engine, sid, "pay?", "You can pay it in the secure checkout below.", "VERIFIED",
                     [{"label": "S1", "tool": "list_bills"}], [card])
    hist.append_turn(engine, sid, "thanks", "You're welcome!", "CONVERSATIONAL", [])        # older/plain format
    msgs = hist.get_session(engine, SUB, sid)["messages"]
    assert msgs[1]["cards"] == [card] and msgs[1]["citations"][0]["label"] == "S1"
    assert msgs[3]["cards"] == [] and msgs[3]["citations"] == []


def test_explanation_json_missing_lists_is_normalised_so_the_bill_page_cannot_crash(world):
    engine, r, _state = world
    cur, prev = row_to_bill(get_bill_row(engine, SUB, "2026-10")), row_to_bill(get_bill_row(engine, SUB, "2026-09"))
    out = explain_bill(scripted_llm(r, [("json", {"summary": "Your bill went up because of roaming in zone Z2."})]),
                       r, cur, prev)
    assert out["status"] == "VERIFIED" and out["explanation"]["drivers"] == [] and out["explanation"]["top_usage"] == []


def test_may_as_a_verb_is_not_read_as_the_may_bill():
    from core_api.agent.retrieve import cycle_hint
    known = ["2026-05", "2026-10"]
    assert cycle_hint("may I pay in parts?", None, known) is None
    assert cycle_hint("why was my bill in May higher?", None, known) == "2026-05"
    assert cycle_hint("october roaming", None, known) == "2026-10"


def test_spine_drops_stale_docs_and_does_not_re_embed_an_unchanged_customer(world):
    from core_api.agent.spine import refresh_subscriber
    from core_api.db import context_docs
    from sqlalchemy import insert as _insert
    from sqlalchemy import select as _select
    engine, _r, state = world
    emb = HashEmbedder()
    first = refresh_subscriber(engine, state, emb, SUB)
    assert first["embedded"] > 0
    with engine.begin() as cx:   # a doc that is no longer generated (e.g. an old month's live usage)
        cx.execute(_insert(context_docs).values(id=f"{SUB}:usage:2025-01", subscriber_id=SUB, doc_type="usage_live",
                                                cycle="2025-01", title="old", body="old", source_ref="x",
                                                content_hash="x", embedding_json="[]"))
    again = refresh_subscriber(engine, state, emb, SUB)
    assert again["embedded"] == 0 and again["removed"] == 1   # nothing changed: no re-embedding; stale doc gone
    with engine.connect() as cx:
        assert cx.execute(_select(context_docs.c.id).where(context_docs.c.id == f"{SUB}:usage:2025-01")).first() is None


def test_polish_space_thousands_separator_is_one_figure():
    """Prod: 'Dlaczego mój rachunek jest wyższy?' ended in the apology; the answer wrote '1 676 MB' (Polish style)
    and the checker split it into '1' and '676'."""
    from core_api.agent.loop import attach_sources, check_numbers
    src = {"S1": '{"data": "1,676 MB of 5,000 MB", "total": "30.75 PLN"}'}
    assert check_numbers("Zużyłeś 1 676 MB z 5 000 MB, rachunek wynosi 30,75 PLN.", src) == []
    assert check_numbers("Zone Z2 372 MB", {"S1": "Z2: 372 MB"}) == []      # 'Z2 372' stays two numbers
    assert "[S1]" in attach_sources("Zużyłeś 1 676 MB.", src)


def test_data_position_breaks_down_plan_addons_and_overage():
    """Prod: after approving a 5 GB add-on the customer was told it 'isn't reflected'. The tool only gave a total
    (40,000 MB); the breakdown (30,000 plan + 2x5 GB add-ons) and the real overage must be explicit."""
    from core_api.agent.tools import _data_position
    from telco_common.catalog import get_plan
    m = get_plan("DE-M")
    counters = {"DATA": 91_719_634_846, "addon:DE-DATA-5GB": 2, "addon_bytes": 10_000_000_000}
    pos = _data_position(m.data_mb, counters)
    assert pos["plan_data"] == "30,000 MB" and pos["total_allowance"] == "40,000 MB"
    assert "x2 (+10,000 MB)" in pos["add_ons_this_month"][0]
    assert pos["data_remaining"] == "0 MB" and pos["over_allowance_by"] == "51,720 MB"
    # one more 5 GB add-on still leaves them over: the proposal must say so, not imply data is back
    assert "over_allowance_by" in _data_position(m.data_mb, counters, extra_mb=5_000)
    light = _data_position(m.data_mb, {"DATA": 20_000 * MB, "ROAMING_DATA:EU": 1_000 * MB})
    assert light["data_used"] == "21,000 MB" and light["data_remaining"] == "9,000 MB" and "over_allowance_by" not in light


def test_addon_proposal_states_its_real_effect_and_orders_tool_reports_status(world):
    """The add-on proposal says honestly whether it gives data back this month; get_orders shows real status."""
    from core_api.agent.tools import TOOLS
    engine, _r, state = world
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder())
    tools = TOOLS if isinstance(TOOLS, dict) else {t.name: t for t in TOOLS}
    from core_api.db import bills
    with engine.begin() as cx:   # an overdue bill blocks orders; this test is about the proposal itself
        cx.execute(bills.update().values(status="PAID"))
    usage = tools["get_current_usage"].run({}, ctx)
    assert {"plan_data", "total_allowance", "data_remaining", "beyond_the_allowance"} <= usage.keys()
    pr = tools["propose_addon"].run({"addon_code": "DE-DATA-5GB"}, ctx)
    assert pr.get("proposal") and "effect" in pr and "card below" in pr["status"] and "checkout" not in json.dumps(pr)
    orders = tools["get_orders"].run({}, ctx)["orders"]
    assert orders[0]["status"] == "prepared, not ordered: waiting for the customer's approval" and "5 GB" in orders[0]["order"]


def test_number_check_is_locale_aware_and_does_not_confuse_9_99_with_999():
    """The old normaliser dropped separators: '999 EUR' matched a 9.99 price; '41,59' (DE) vs '4,159' (EN)."""
    from core_api.agent.agent import numbers_in, supported_by
    src = numbers_in('{"price": "9.99 EUR", "data": "1,676 MB", "total": "41.59 EUR"}')
    assert not supported_by("999", src) and supported_by("9,99", src) and supported_by("9.99", src)
    assert supported_by("41,59", src) and supported_by("41", src) and not supported_by("4159", src)
    assert supported_by("1.676", src) and supported_by("1,676", src)


def test_every_turn_starts_from_a_fresh_account_snapshot_with_the_catalog(world):
    """Prod: the assistant offered 'a bigger plan' it never checked (M was already the largest), and answered
    from earlier turns until pushed. Each turn now begins with usage, bills, orders and the catalog."""
    engine, r, state = world
    llm = scripted_llm(r, [("text", "You're already on the largest plan.")])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    events = collect(run_stream("is there a bigger plan?", ctx, llm, engine))
    calls = [t["tool"] for t in events[-1]["trace"]["tool_calls"]]
    assert calls[:4] == ["get_current_usage", "list_bills", "get_orders", "get_plans_and_addons"]
    assert next(e["label"] for e in events if e["type"] == "step") == "Looking at your account"
    from core_api.agent.tools import TOOLS
    cat = TOOLS["get_plans_and_addons"].run({}, ctx)
    assert all("PL" not in a["code"] for a in cat["add_ons"]) and cat["largest_plan"]


def test_catalog_prices_this_month_for_a_switch_today(world):
    """Prod: 'what would I pay this month if I switch?' -> 'the tools don't compute a combined total'. The
    catalog now re-rates the month with the new plan from today (same rating path as the bill)."""
    from core_api.agent.tools import TOOLS
    engine, _r, state = world
    cat = TOOLS["get_plans_and_addons"].run({}, ToolContext(SUB, "DE", engine, state, HashEmbedder()))
    others = [p for p in cat["plans"] if not p.get("current")]
    assert others and all(p["this_month_total_if_switched_today"].endswith("EUR") for p in others)
    assert "today" in cat["rules"]["tariff_change"] and "pro-rated" in cat["rules"]["tariff_change"]


def test_answer_written_alongside_a_payment_card_is_kept(world):
    """Prod (PL): the model explained the bill and prepared the payment in the same turn; the follow-up said
    only 'the payment is ready below' and the whole explanation was dropped. It is kept now."""
    engine, r, state = world
    total = row_to_bill(get_bill_row(engine, SUB, "2026-10")).total
    explanation = (f"Your October bill is {total / 100:.2f} EUR. Most of it is roaming in zone Z2, 372 MB of data "
                   "outside the EU, which wasn't there the month before.")
    llm = scripted_llm(r, [
        ("tools", [("get_bill", {"cycle": "2026-10"})]),
        ("text_then_tools", (explanation, [("propose_payment", {"purpose": "bill"})])),
        ("text", "You can pay it in the secure checkout below."),
    ])
    events = collect(run_stream("Why is my bill higher?", ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm), llm, engine))
    done = events[-1]
    assert "372 MB" in done["answer"] and "checkout below" in done["answer"]
    assert done["answer"].index("372 MB") < done["answer"].index("checkout below")   # the answer first
    assert [e["kind"] for e in events if e["type"] == "proposal"] == ["PAYMENT"]
    assert "".join(e["text"] for e in events if e["type"] == "token") == done["answer"]


def test_one_open_offer_per_kind_and_expired_offers_are_not_waiting(world):
    """Prod conversation 23 Nov: a 20 GB offer from an earlier chat stayed "waiting for approval" next to the one
    the customer then bought, and the assistant kept bringing it up after "no more add-ons"."""
    from core_api.agent.tools import TOOLS
    from core_api.db import bills, orders
    from core_api.workflows.orders import propose_addon, propose_plan_change
    from sqlalchemy import select
    engine, r, state = world
    with engine.begin() as cx:
        cx.execute(bills.update().values(status="PAID"))
    first = propose_addon(engine, SUB, "DE-DATA-20GB", source="assistant")
    plan = propose_plan_change(engine, SUB, "DE-M", source="app")
    second = propose_addon(engine, SUB, "DE-DATA-20GB", source="assistant")
    with engine.connect() as cx:
        st = dict(cx.execute(select(orders.c.id, orders.c.status)).all())
    assert st[first["order_id"]] == "EXPIRED" and st[second["order_id"]] == "PROPOSED"
    assert st[plan["order_id"]] == "PROPOSED"                       # another kind is untouched
    llm = scripted_llm(r, [])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    listed = TOOLS["get_orders"].run({}, ctx)["orders"]
    assert sum("waiting" in o["status"] for o in listed if "20 GB" in o["order"]) == 1
    assert any(o["status"] == "offer expired" for o in listed)


def test_money_is_labelled_with_vat_and_advances_are_not_charged_twice(world):
    from core_api.agent.tools import TOOLS
    from core_api.db import payments
    engine, r, state = world
    llm = scripted_llm(r, [])
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), llm)
    cat = TOOLS["get_plans_and_addons"].run({}, ctx)
    assert all(p["monthly_fee"].endswith("incl. VAT)") and "+ VAT" in p["monthly_fee"] for p in cat["plans"])
    usage = TOOLS["get_current_usage"].run({}, ctx)
    assert {"unbilled_lines_before_vat", "unbilled_subtotal_before_vat", "vat", "unbilled_total_incl_vat"} <= usage.keys()
    full = TOOLS["propose_payment"].run({"purpose": "advance"}, ctx)
    total = round(float(full["amount"].split()[0]) * 100)
    with engine.begin() as cx:   # the customer pays part of the month in advance
        cx.execute(payments.insert().values(id="adv-1", subscriber_id=SUB, purpose="ADVANCE", status="PAID",
                                            amount=total - 500, currency="EUR", gateway="razorpay", idempotency_key="adv-1",
                                            created_at=datetime.now(UTC)))
    left = TOOLS["propose_payment"].run({"purpose": "advance"}, ctx)
    assert left["amount"] == "5.00 EUR" and "already_paid_in_advance" in left
    assert "advance_paid_this_month" in TOOLS["get_current_usage"].run({}, ctx)


def test_migration_retires_open_offers_that_a_newer_order_replaced(world):
    from datetime import timedelta

    from core_api.db import _expire_superseded_offers, orders
    from sqlalchemy import select
    engine, _r, _s = world
    t0 = datetime.now(UTC)
    rows = [("old-offer", "ADDON", "PROPOSED", t0 - timedelta(hours=2)), ("bought", "ADDON", "COMPLETED", t0 - timedelta(hours=1)),
            ("open-plan", "PLAN", "PROPOSED", t0 - timedelta(hours=3)), ("newest-offer", "ADDON", "PROPOSED", t0)]
    with engine.begin() as cx:
        for oid, kind, st, ts in rows:
            cx.execute(orders.insert().values(id=oid, subscriber_id=SUB, kind=kind, addon_code="DE-DATA-20GB" if kind == "ADDON" else "DE-M",
                                              price=1999, currency="EUR", status=st, source="assistant", created_at=ts, updated_at=ts))
        _expire_superseded_offers(cx)
        st = dict(cx.execute(select(orders.c.id, orders.c.status)).all())
    assert st == {"old-offer": "EXPIRED", "bought": "COMPLETED", "open-plan": "PROPOSED", "newest-offer": "PROPOSED"}


def test_chat_history_has_utc_timestamps_for_chats_and_messages(world):
    """The panel shows when each question was asked and answered; naive datetimes would be read as local time."""
    from core_api.agent import history as hist
    engine, _r, _s = world
    sid, _ = hist.ensure_session(engine, SUB, None)
    hist.append_turn(engine, sid, "How much data is left?", "About 50 MB [S1].", "VERIFIED", [], [])
    s = hist.get_session(engine, SUB, sid)
    assert [m["role"] for m in s["messages"]] == ["user", "assistant"]
    assert all(m["at"].endswith("+00:00") for m in s["messages"])
    assert hist.list_sessions(engine, SUB)[0]["updated_at"].endswith("+00:00")


def test_catalog_states_each_add_on_effect_this_month(world):
    """Prod 23 Nov: with orders blocked (no proposal), the model said a 5 GB add-on would leave the customer over
    although 6,084 MB < 3,000 + 5,000 MB. The catalog now carries the computed effect per add-on."""
    from core_api.agent.tools import TOOLS
    engine, r, state = world
    ctx = ToolContext(SUB, "DE", engine, state, HashEmbedder(), scripted_llm(r, []))
    for a in TOOLS["get_plans_and_addons"].run({}, ctx)["add_ons"]:
        assert a["effect_this_month"].startswith("allowance would be ") and (" left" in a["effect_this_month"] or "still over" in a["effect_this_month"])
