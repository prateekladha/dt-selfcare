"""Tariff change mid-cycle: fees and allowances pro-rated by day; history-driven; idempotent."""
import asyncio
from datetime import UTC, datetime

import fakeredis
import pytest
from core_api import deps
from core_api.db import create_schema, plan_changes, set_engine
from sqlalchemy import create_engine, insert
from sqlalchemy.pool import StaticPool
from telco_common.billing import prorated_plan, rate
from telco_common.catalog import MB, get_plan
from telco_common.models import UsageType

SUB = "4915100000002"  # DE-FLEX


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("APP_SEED_DEMO", "true")
    monkeypatch.setenv("APP_DEMO_PASSWORD", "demo-pass-123")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_schema(engine)
    set_engine(engine)
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(deps, "get_valkey", lambda: r)
    from core_api.seed import seed_demo
    from telco_common.usage_state import UsageState

    seed_demo(engine, UsageState(r, 4))
    return engine, r


def test_fees_and_allowances_prorated_by_day():
    flex, m = get_plan("DE-FLEX"), get_plan("DE-M")   # 9.95 / 3 GB / 200 min / 100 SMS  ->  34.95 / 30 GB / unlimited
    eff, lines = prorated_plan([(flex, 20), (m, 10)])
    assert [x.amount for x in lines] == [663, 1165]            # 995*20/30, 3495*10/30
    assert lines[0].label == "Mobile Flex monthly fee, 20 of 30 days"
    assert eff.data_mb == 12_000                              # (3000*20 + 30000*10) / 30
    assert eff.voice_min is None and eff.sms is None          # unlimited in one segment
    assert eff.overage_policy == m.overage_policy             # usage priced at the current tariff


def test_bill_with_change_has_two_fee_lines_and_blended_allowance():
    flex, m = get_plan("DE-FLEX"), get_plan("DE-M")
    counters = {str(UsageType.DATA): 10_000 * MB}             # 10 GB: over Flex's 3 GB, under the blended 12 GB
    whole_flex = rate(SUB, "2026-11", flex, counters)
    prorated = rate(SUB, "2026-11", m, counters, segments=[(flex, 20), (m, 10)])
    assert any(x.code == "DATA_OVERAGE" for x in whole_flex.lines)
    assert not any(x.code == "DATA_OVERAGE" for x in prorated.lines)
    assert [x.code for x in prorated.lines if x.category == "PLAN"] == ["PLAN_FEE:1:DE-FLEX", "PLAN_FEE:2:DE-M"]
    single = rate(SUB, "2026-11", m, counters, segments=[(m, 30)])
    assert [x.code for x in single.lines if x.category == "PLAN"] == ["PLAN_FEE"]


def test_segments_from_history(world):
    from core_api.services.plan_history import segments

    engine, _ = world
    with engine.begin() as cx:
        cx.execute(insert(plan_changes).values(order_id="o1", subscriber_id=SUB, from_plan="DE-FLEX", to_plan="DE-M",
                                               effective_at=datetime(2026, 9, 21, 14, 0, tzinfo=UTC)))
        cx.execute(insert(plan_changes).values(order_id="o2", subscriber_id=SUB, from_plan="DE-M", to_plan="DE-S",
                                               effective_at=datetime(2026, 9, 26, 9, 0, tzinfo=UTC)))
        cx.execute(insert(plan_changes).values(order_id="o3", subscriber_id=SUB, from_plan="DE-S", to_plan="DE-M",
                                               effective_at=datetime(2026, 8, 1, 0, 5, tzinfo=UTC)))
    seg = segments(engine, SUB, "2026-09", "DE-S")
    assert [(p.code, d) for p, d in seg] == [("DE-FLEX", 20), ("DE-M", 5), ("DE-S", 5)]   # two switches in one cycle
    assert segments(engine, SUB, "2026-08", "DE-M") is None    # switched on day 1: whole cycle on the new tariff
    assert segments(engine, SUB, "2026-07", "DE-FLEX") is None


def test_apply_plan_records_history_once_and_bill_run_prorates(world):
    from core_api.services.bills import close_cycle, unbilled
    from core_api.workflows.addon import apply_plan
    from telco_common.usage_state import UsageState

    engine, r = world
    asyncio.run(apply_plan("ord-1", SUB, "DE-M"))
    asyncio.run(apply_plan("ord-1", SUB, "DE-M"))             # retried activity: no second history row
    with engine.connect() as cx:
        rows = cx.execute(plan_changes.select()).all()
    assert len(rows) == 1 and (rows[0].from_plan, rows[0].to_plan) == ("DE-FLEX", "DE-M")
    now = datetime.now(UTC)
    cur = unbilled(UsageState(r, 4), SUB, "DE-M", now, engine)
    fee_lines = [x for x in cur.lines if x.category == "PLAN"]
    if now.day > 1:                                           # switched today: old tariff for the days before
        assert [x.code.split(":")[-1] for x in fee_lines] == ["DE-FLEX", "DE-M"]
    # a closed historical cycle with a recorded mid-cycle change is billed pro-rated
    with engine.begin() as cx:
        cx.execute(insert(plan_changes).values(order_id="o-hist", subscriber_id=SUB, from_plan="DE-FLEX", to_plan="DE-M",
                                               effective_at=datetime(2026, 9, 11, tzinfo=UTC)))
    close_cycle(engine, UsageState(r, 4), "2026-09", None)
    from core_api.services.bills import get_bill_row, row_to_bill
    bill = row_to_bill(get_bill_row(engine, SUB, "2026-09"))
    assert [x.label for x in bill.lines if x.category == "PLAN"] == [
        "Mobile Flex monthly fee, 10 of 30 days", "Mobile M monthly fee, 20 of 30 days"]
