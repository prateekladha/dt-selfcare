"""Policy judge: strict parsing, calibration math, the labelled set, the eval gate, online sampling, quality trend."""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import fakeredis
import httpx
import pytest
from core_api import evals
from core_api.agent import judge as J
from core_api.agent import loop
from core_api.agent.llm import LLM
from core_api.db import agent_traces, answer_judgements, create_schema, eval_runs
from core_api.routers import system
from core_api.settings import Settings
from sqlalchemy import create_engine, insert, select
from sqlalchemy.pool import StaticPool


@pytest.fixture
def engine():
    e = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_schema(e)
    return e


def judge_llm(reply: str, seen: list, provider: str = "gateway", sample_rate: float = 1.0):
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"model": "mistral-small", "choices": [{"message": {"content": reply}}],
                                         "usage": {"prompt_tokens": 900, "completion_tokens": 40}})
    s = Settings(llm_gateway_url="http://gw", llm_gateway_key="k", deepseek_api_key="k", database_url="sqlite://",
                 judge_sample_rate=sample_rate)
    t = httpx.MockTransport(handler)
    return LLM(s, fakeredis.FakeRedis(), provider, http=httpx.Client(transport=t), ahttp=httpx.AsyncClient(transport=t))


def test_parse_is_strict():
    assert J.parse('{"verdict":"PASS","violations":[],"reason":"ok"}').verdict == "PASS"
    j = J.parse('```json\n{"verdict":"FAIL","violations":["r1","R99"],"reason":"next month"}\n```')
    assert (j.verdict, j.violations) == ("FAIL", ["R1"])                     # unknown ids dropped, case-insensitive
    assert J.parse('{"verdict":"FAIL","violations":[]}').verdict == "ERROR"   # a FAIL must name a rule
    assert J.parse('{"verdict":"PASS","violations":["R3"]}').verdict == "FAIL"  # the rule list decides
    assert J.parse("I think it is fine").verdict == "ERROR"                   # never counted as a pass
    assert J.parse('{"verdict":"MAYBE"}').verdict == "ERROR"


def test_agreement_kappa_and_fail_recall():
    perfect = [("PASS", "PASS"), ("FAIL", "FAIL")] * 5
    assert J.agreement(perfect)["kappa"] == 1.0
    # 10 items: human 5 FAIL / 5 PASS; judge misses one FAIL and flags one PASS -> po 0.8, pe 0.5, kappa 0.6
    pairs = [("FAIL", "FAIL")] * 4 + [("FAIL", "PASS")] + [("PASS", "PASS")] * 4 + [("PASS", "FAIL")]
    m = J.agreement(pairs)
    assert (m["accuracy"], m["kappa"], m["fail_recall"], m["fail_precision"]) == (0.8, 0.6, 0.8, 0.8)
    assert J.agreement([("FAIL", "ERROR"), ("PASS", "PASS")])["fail_recall"] == 0.0   # an ERROR is a miss
    assert J.agreement([])["kappa"] is None


def test_majority_vote_smooths_a_noisy_judge():
    P, F = J.Judgement("PASS"), lambda *r: J.Judgement("FAIL", list(r))
    E = J.Judgement("ERROR", reason="timeout")
    assert J.vote([P, F("R4"), P]).verdict == "PASS"                                   # one stray FAIL
    v = J.vote([F("R7", "R2"), F("R7"), P])
    assert (v.verdict, v.violations) == ("FAIL", ["R7"])                               # rules most fails named
    assert J.vote([E, E, P]).verdict == "ERROR"                                        # mostly errors: no verdict
    assert J.vote([E, F("R1"), F("R1")]).verdict == "FAIL"
    assert J.vote([E, P, F("R1")]).verdict == "PASS"                                   # tie: not a majority to fail


def test_labelled_set_is_balanced_and_names_known_rules():
    labels = evals.load_labels()
    assert len(labels) >= 20
    fails = [x for x in labels if x["human"] == "FAIL"]
    assert 0.35 <= len(fails) / len(labels) <= 0.65                   # both classes, or kappa means nothing
    assert all(x["rules"] and set(x["rules"]) <= set(J.RULES) for x in fails)
    assert {r for x in fails for r in x["rules"]} == set(J.RULES)       # every rule has a negative example
    assert len({x["id"] for x in labels}) == len(labels)


def test_judge_uses_its_own_model_alias_and_reports_cost():
    seen = []
    llm = judge_llm('{"verdict":"FAIL","violations":["R1"],"reason":"says next month"}', seen)
    j = J.judge(llm, "When does it start?", "From next month.", "[S4 get_plans_and_addons] {...}")
    assert (j.verdict, j.violations, j.model) == ("FAIL", ["R1"], "mistral-small")
    assert seen[0]["model"] == "judge-llm" and seen[0]["temperature"] == 0.0
    assert seen[0]["response_format"] == {"type": "json_object"}
    assert "R1:" in seen[0]["messages"][1]["content"] and "From next month." in seen[0]["messages"][1]["content"]


def test_judge_outage_is_an_error_not_a_verdict():
    def boom(req):
        return httpx.Response(500, json={})
    s = Settings(llm_gateway_url="http://gw", llm_gateway_key="k", database_url="sqlite://")
    llm = LLM(s, fakeredis.FakeRedis(), "gateway", http=httpx.Client(transport=httpx.MockTransport(boom)))
    assert J.judge(llm, "q", "a", "").verdict == "ERROR"


def case(**kw):
    base = {"id": "x", "who": "Jonas", "q": "q", "status": "VERIFIED", "grounded": True, "mentions": 1.0, "tools_ok": True,
            "judge": "PASS", "violations": [], "judge_reason": "", "latency_ms": 3000, "cost_usd": 0.002, "answer": "a"}
    return base | kw


def test_gate_uses_the_judge_only_while_it_is_calibrated():
    cases = [case()] * 6 + [case(judge="FAIL", violations=["R1"])] * 4         # judge pass rate 0.6
    assert "judge_pass_rate" in evals.summarize(cases, judge_trusted=True)["failed"]
    assert evals.summarize(cases, judge_trusted=False)["failed"] == []           # uncalibrated judge: reported, not gating
    many_errors = [case()] * 4 + [case(judge="ERROR")] * 6 + [case(judge="FAIL", violations=["R4"])]
    assert "judge_pass_rate" not in evals.summarize(many_errors, True)["failed"]  # judge outage doesn't block a deploy
    ungrounded = [case()] * 8 + [case(status="UNVERIFIED", grounded=False)] * 2
    m = evals.summarize(ungrounded, True)
    assert m["failed"] == ["grounded_rate"] and m["grounded_rate"] == 0.8 and m["p95_ms"] == 3000


def test_production_answers_are_sampled_for_the_judge_off_the_response_path(engine):
    seen = []
    llm = judge_llm('{"verdict":"FAIL","violations":["R2"],"reason":"charged now"}', seen)

    async def go():
        loop.sample_for_judge(llm, engine, "t-1", "VERIFIED", "When is it charged?", "Immediately.", [
            {"label": "S1", "tool": "get_plans_and_addons", "body": '{"rules":{"add_on":"charged on the next bill"}}'}])
        loop.sample_for_judge(llm, engine, "t-2", "CONVERSATIONAL", "Hi", "Hello!", [])   # small talk: skipped
        await asyncio.gather(*list(loop._JUDGING))
    asyncio.run(go())
    with engine.connect() as cx:
        rows = cx.execute(select(answer_judgements)).all()
    assert [(r.trace_id, r.verdict, r.violations) for r in rows] == [("t-1", "FAIL", "R2")]
    assert "charged on the next bill" in seen[0]["messages"][1]["content"]

    llm0 = judge_llm("{}", [], sample_rate=0.0)

    async def none():
        loop.sample_for_judge(llm0, engine, "t-3", "VERIFIED", "q", "a", [])
        assert not loop._JUDGING
    asyncio.run(none())


def test_quality_trend_per_day_with_judge_pass_rate(engine, monkeypatch):
    now = datetime.now(UTC)
    rows = [("a", "VERIFIED", 2000, 0.002, now), ("b", "REPAIRED", 4000, 0.004, now), ("c", "UNVERIFIED", 9000, 0.006, now),
            ("d", "VERIFIED", 3000, 0.002, now - timedelta(days=8))]
    with engine.begin() as cx:
        for tid, st, ms, cost, ts in rows:
            cx.execute(insert(agent_traces).values(id=tid, subscriber_id="s", question="q", status=st, answer="x",
                                                   trace=json.dumps({"latency_ms": ms}), cost_usd=str(cost), created_at=ts))
        for tid, v, viol in (("a", "PASS", ""), ("b", "FAIL", "R1,R3")):
            cx.execute(insert(answer_judgements).values(trace_id=tid, verdict=v, violations=viol, reason="", judge_model="m",
                                                        judge_version=J.JUDGE_VERSION, cost_usd="0", created_at=now))
        cx.execute(insert(eval_runs).values(id="r1", kind="calibration", prompt_version="-", judge_version=J.JUDGE_VERSION,
                                            passed=1, metrics=json.dumps({"n": 24, "kappa": 0.83, "trusted": True}),
                                            cases="[]", created_at=now))
    monkeypatch.setattr(system, "get_engine", lambda: engine)
    q = system.assistant_quality(14)
    today = q["days"][-1]
    assert len(q["days"]) == 14 and today["day"] == now.date().isoformat()
    assert (today["answers"], today["verified_rate"], today["repaired_rate"], today["unverified_rate"]) == (3, 0.667, 0.333, 0.333)
    assert (today["p50_ms"], today["p95_ms"], today["judged"], today["judge_pass_rate"]) == (4000, 9000, 2, 0.5)
    assert q["last7"]["answers"] == 3 and q["prev7"]["answers"] == 1
    assert q["violations_7d"] == {"R1": 1, "R3": 1}
    assert q["calibration"]["kappa"] == 0.83 and q["calibration"]["trusted"] is True


def test_tool_use_means_the_answer_rests_on_cited_account_data():
    """Facts first: a question about general rules is answered from the start-of-turn snapshot (no own lookup);
    it passes when it cites the customer's records, and fails when it cites none."""
    case_ = {"id": "mia-addon-billing", "who": "Mia", "q": "How are add-ons billed?", "must": [], "tools": 1}
    out = {"answer": "Charged on your next bill [S4].", "status": "VERIFIED", "tool_calls": 0, "cost_usd": 0.0, "latency_ms": 1,
           "citations": [{"label": "S4", "tool": "get_plans_and_addons", "body": "{}"}]}
    assert evals.score(case_, out, None)["tools_ok"] and evals.score(case_, out, None)["own_lookups"] == 0
    assert not evals.score(case_, out | {"citations": []}, None)["tools_ok"]
