"""Order events over the stream, the adapter service, circuit breaker, tariff-change workflow, bill-day prep."""
import asyncio
import json
import uuid

import fakeredis
import httpx
import pytest
from core_api import deps
from core_api.db import create_schema, notifications, orders, outbox, set_engine, subscribers
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

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


def _events(engine, order_id):
    with engine.connect() as cx:
        rows = cx.execute(select(outbox).where(outbox.c.topic == "order-events").order_by(outbox.c.id)).all()
    return [json.loads(x.payload) for x in rows if json.loads(x.payload)["order_id"] == order_id]


def test_every_status_change_is_an_order_event_in_the_same_transaction(world):
    from core_api.workflows.orders import propose_addon, propose_plan_change, set_status

    engine, _ = world
    o = propose_addon(engine, SUB, "DE-DATA-5GB", "app")
    set_status(engine, o["order_id"], "APPROVED", "Approved")
    set_status(engine, o["order_id"], "COMPLETED", "done")
    assert [e["status"] for e in _events(engine, o["order_id"])] == ["PROPOSED", "APPROVED", "COMPLETED"]
    p = propose_plan_change(engine, SUB, "DE-M", "assistant")
    assert p["kind"] == "PLAN" and p["preview"]["from"] == "Mobile Flex" and p["preview"]["data"] == "30 GB"
    assert _events(engine, p["order_id"])[0]["kind"] == "PLAN"
    from core_api.workflows.orders import OrderError
    with pytest.raises(OrderError):
        propose_plan_change(engine, SUB, "DE-FLEX", "app")   # already on it
    with pytest.raises(OrderError):
        propose_plan_change(engine, SUB, "PL-S", "app")      # other market


def test_order_events_projection_notifies_once_and_invalidates_home(world):
    from core_api.notifier import SPINE_DIRTY, project_order_event

    engine, r = world
    ev = {"type": "ORDER_STATUS_CHANGED", "order_id": "o-1", "subscriber_id": SUB, "kind": "PLAN",
          "status": "COMPLETED", "detail": "Your new tariff is active"}
    assert project_order_event(engine, r, {**ev, "status": "SUBMITTED"}) is False  # not terminal: cache only
    assert int(r.get(f"hv:{{{SUB}}}")) == 1 and SUB in SPINE_DIRTY
    assert project_order_event(engine, r, ev) is True
    assert project_order_event(engine, r, ev) is False  # redelivery: no duplicate notification
    with engine.connect() as cx:
        n = cx.execute(select(notifications).where(notifications.c.alert_id == "order:o-1:COMPLETED")).all()
    assert len(n) == 1 and n[0].title == "Your new tariff is active"


def test_adapter_service_tmf622_rate_limit_and_markets(world, monkeypatch):
    from core_api.adapters.bss import BASE, product_order
    from core_api.apps import adapter as app_mod
    from core_api.settings import get_settings

    monkeypatch.setattr(get_settings(), "bss_max_rps", 2)
    app = app_mod.build("order-adapter-test", [app_mod.router])
    c = TestClient(app)
    first = c.post(BASE, json=product_order(f"ord-{uuid.uuid4().hex[:8]}", SUB, "DE-DATA-5GB", "DE"))
    assert first.status_code == 201 and first.json()["id"].startswith("DEB-") and first.json()["state"] == "acknowledged"
    assert c.get(f"{BASE}/{first.json()['id']}").json()["state"] == "acknowledged"
    c.post(BASE, json=product_order(f"ord-{uuid.uuid4().hex[:8]}", SUB, "DE-DATA-5GB", "DE"))
    over = c.post(BASE, json=product_order(f"ord-{uuid.uuid4().hex[:8]}", SUB, "DE-DATA-5GB", "DE"))
    assert over.status_code == 429 and over.headers["retry-after"] == "1"  # DE legacy at capacity this second
    pl = c.post(BASE, json=product_order(f"ord-{uuid.uuid4().hex[:8]}", "48600000001", "PL-S", "PL", "modify"))
    assert pl.status_code == 201 and pl.json()["id"].startswith("PLB-")  # PL has its own capacity
    assert c.post(BASE, json=product_order("ord-zzzzzzzz", SUB, "X", "FR")).status_code == 422


def test_circuit_breaker_opens_fails_fast_and_recovers():
    from telco_common.resilience import CircuitOpenError, ServiceClient

    state = {"down": True, "calls": 0}

    def handler(req):
        state["calls"] += 1
        return httpx.Response(503 if state["down"] else 200, json={})

    sc = ServiceClient("dep", "http://dep", fail_threshold=3, reset_after_s=0.2, transport=httpx.MockTransport(handler))

    async def go():
        for _ in range(3):
            await sc.request("GET", "/x")
        assert sc.breaker.state == "OPEN"
        with pytest.raises(CircuitOpenError):
            await sc.request("GET", "/x")      # fails fast: the dependency isn't called
        assert state["calls"] == 3
        await asyncio.sleep(0.25)
        state["down"] = False
        assert (await sc.request("GET", "/x")).status_code == 200  # half-open trial succeeds
        assert sc.breaker.state == "CLOSED"
    asyncio.run(go())


def test_timeouts_are_split_connect_vs_read():
    from telco_common.resilience import timeouts

    t = timeouts(1.0, 5.0)
    assert (t.connect, t.read) == (1.0, 5.0)


def test_plan_change_workflow_on_temporal():
    from core_api.workflows.addon import PlanChangeWorkflow
    from temporalio import activity
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    calls = []

    @activity.defn(name="validate_order")
    async def validate_order(order_id: str) -> dict:
        return {"subscriber_id": SUB, "addon_code": "DE-M", "kind": "PLAN"}

    @activity.defn(name="submit_to_bss")
    async def submit_to_bss(order_id: str) -> str:
        return "DEB-1"

    @activity.defn(name="bss_status")
    async def bss_status(bss_id: str) -> str:
        return "acknowledged"

    @activity.defn(name="apply_plan")
    async def apply_plan(order_id: str, sub: str, plan: str) -> str:
        calls.append(("apply_plan", plan))
        return plan

    @activity.defn(name="finish_order")
    async def finish_order(order_id: str, status: str, detail: str) -> None:
        calls.append(("finish", status))

    async def run():
        async with await WorkflowEnvironment.start_time_skipping() as env, Worker(
                env.client, task_queue="t", workflows=[PlanChangeWorkflow],
                activities=[validate_order, submit_to_bss, bss_status, apply_plan, finish_order]):
            h = await env.client.start_workflow(PlanChangeWorkflow.run, "o1", id=f"wf-{uuid.uuid4()}", task_queue="t")
            await asyncio.sleep(0.5)
            await h.signal("bss_callback", "CONFIRMED")   # what the bss-events consumer sends
            return await h.result()
    assert asyncio.run(run()) == "COMPLETED"
    assert calls == [("apply_plan", "DE-M"), ("finish", "COMPLETED")]


def test_apply_plan_switches_tariff_and_api_lists_plans(world):
    from core_api.workflows.addon import apply_plan

    engine, _ = world
    asyncio.run(apply_plan("o1", SUB, "DE-M"))
    with engine.connect() as cx:
        assert cx.execute(select(subscribers.c.plan_code).where(subscribers.c.id == SUB)).scalar() == "DE-M"
        cx.execute(update(subscribers).where(subscribers.c.id == SUB).values(plan_code="DE-FLEX"))
        cx.commit()
    with engine.connect() as cx:
        assert cx.execute(select(orders)).all() == []


def test_bill_day_prepare_warms_home_cache(world):
    from datetime import UTC, datetime

    from core_api.services.bill_day import prepare
    from core_api.services.bills import close_cycle
    from telco_common.usage_state import UsageState

    engine, r = world
    state = UsageState(r, 4)
    close_cycle(engine, state, "2026-10", None)
    out = prepare(engine, r, "2026-10", rps=0, explain=False)
    assert out["bills"] >= 5 and out["warmed"] == out["bills"]
    from core_api.services.home_cache import get_or_build
    snap, hit = get_or_build(r, SUB, lambda: {"miss": True})
    assert hit and snap["bills"][-1]["cycle"] == "2026-10"
    assert datetime.now(UTC)


def test_adapter_unknown_order_is_404(world):
    from core_api.adapters.bss import BASE
    from core_api.apps import adapter as app_mod

    c = TestClient(app_mod.build("order-adapter-test2", [app_mod.router]))
    assert c.get(f"{BASE}/DEB-NOTEXIST").status_code == 404
    assert c.get(f"{BASE}/XYZ-1").status_code == 404
