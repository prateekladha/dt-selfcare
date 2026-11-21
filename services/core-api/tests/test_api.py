"""End-to-end API tests: SQLite + fakeredis stand in for ADB + Valkey, a fake gateway for Razorpay."""
import hashlib
import hmac
import json
from datetime import UTC, datetime

import fakeredis
import pytest
from core_api import deps
from core_api.db import create_schema, set_engine
from core_api.routers import payments as pay_router
from core_api.services.gateway import GatewayOrder
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from telco_common.catalog import MB
from telco_common.models import Country, UsageRecord, UsageType
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState

SUB, OTHER = "4915100000002", "4915100000001"  # traveller (DE-FLEX), streamer (DE-M)


class FakeGateway:
    name, key_id, secret = "razorpay", "rzp_test_x", "s3cret"

    def __init__(self):
        self.orders, self.captured = 0, set()

    def create_order(self, amount, currency, receipt, notes):
        self.orders += 1
        self.last = (amount, currency)
        return GatewayOrder(f"order_{self.orders}", amount, currency, {"provider": "razorpay", "order_id": f"order_{self.orders}"})

    def verify_client_signature(self, order_id, payment_id, signature):
        exp = hmac.new(self.secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(exp, signature)

    def verify_webhook(self, body, signature):
        return signature == "good"

    def is_order_paid(self, order_id):
        return (order_id in self.captured), f"pay_{order_id}"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("APP_SEED_DEMO", "true")
    monkeypatch.setenv("APP_DEMO_PASSWORD", "demo-pass-123")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_schema(engine)
    set_engine(engine)
    r = fakeredis.FakeRedis()
    monkeypatch.setattr(deps, "get_valkey", lambda: r)
    import core_api.routers.auth as auth_mod
    import core_api.routers.me as me_mod
    for mod in (auth_mod, me_mod):
        monkeypatch.setattr(mod, "get_valkey", lambda: r, raising=False)
    state = UsageState(r, 4)
    for mod in (me_mod, pay_router):
        monkeypatch.setattr(mod, "get_usage_state", lambda: state)
    import core_api.main as main_mod
    monkeypatch.setattr(main_mod, "get_usage_state", lambda: state)
    gw = FakeGateway()
    monkeypatch.setattr(pay_router, "gateway_for", lambda *_: gw)
    monkeypatch.setitem(pay_router._gateways, "razorpay", gw)
    with TestClient(main_mod.app) as client:
        yield client, engine, state, gw


def login(client, sub=SUB):
    r = client.post("/v1/auth/login", json={"subscriber_id": sub, "password": "demo-pass-123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def add_usage(state, sub, cycle_dt, counters):
    p = partition_for(sub, 4)
    recs = []
    for i, (t, zone, q) in enumerate(counters):
        recs.append(UsageRecord(record_id=f"{sub}-{cycle_dt:%Y%m}-{i:05d}", subscriber_id=sub, country=Country.DE,
                                usage_type=t, roaming_zone=zone, quantity=q, event_time=cycle_dt))
    off = (state.committed_offset(p) or 0) + 1
    state.apply(p, off, f"m-{sub}-{off}", recs, [50, 80, 100])


def issue(engine, state, cycle, settle_before=None):
    from core_api.services.bills import close_cycle
    return close_cycle(engine, state, cycle, settle_before)


def test_auth_required_and_bad_password(env):
    client, *_ = env
    assert client.get("/v1/me/home").status_code == 401
    assert client.post("/v1/auth/login", json={"subscriber_id": SUB, "password": "nope"}).status_code == 401


def test_home_snapshot_bills_explanation_and_pdf(env):
    client, engine, state, _ = env
    add_usage(state, SUB, datetime(2026, 9, 10, tzinfo=UTC), [(UsageType.DATA, None, 1_000 * MB)])
    add_usage(state, SUB, datetime(2026, 10, 10, tzinfo=UTC), [(UsageType.DATA, None, 2_000 * MB),
                                                               (UsageType.ROAMING_DATA, "Z2", 500 * MB)])
    add_usage(state, SUB, datetime.now(UTC), [(UsageType.DATA, None, 2_600 * MB)])
    assert issue(engine, state, "2026-09", settle_before="2026-10") == {"created": 6, "skipped_existing": 0}
    assert issue(engine, state, "2026-10")["created"] == 6
    assert issue(engine, state, "2026-10")["skipped_existing"] == 6  # bill run is idempotent

    h = login(client)
    home = client.get("/v1/me/home", headers=h)
    assert home.status_code == 200 and home.headers["cache-control"] == "private, no-store"
    body = home.json()
    assert body["usage"]["data_pct"] > 80 and body["unbilled"]["currency"] == "EUR"
    assert [b["cycle"] for b in body["trend"]] == ["2026-09", "2026-10"]
    assert body["trend"][0]["status"] == "PAID" and body["outstanding"]["bill_id"] == f"{SUB}:2026-10"

    ex = client.get("/v1/me/bills/2026-10/explanation", headers=h).json()
    assert ex["provider"] == "template"  # AI flag defaults to off -> deterministic fallback
    assert ex["explanation"]["drivers"][0]["code"] == "ROAMING_DATA:Z2"
    assert "increased" in ex["narrative"]

    pdf = client.get("/v1/me/bills/2026-10/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_customer_cannot_read_or_pay_another_customers_bill(env):
    client, engine, state, _ = env
    issue(engine, state, "2026-10")
    h = login(client, SUB)
    r = client.post("/v1/me/payments", json={"purpose": "BILL", "bill_id": f"{OTHER}:2026-10"},
                    headers=h | {"Idempotency-Key": "k-12345678"})
    assert r.status_code == 404


def test_payment_is_idempotent_and_settles_once_from_both_paths(env):
    client, engine, state, gw = env
    issue(engine, state, "2026-10")
    h = login(client)
    hdr = h | {"Idempotency-Key": "tap-00000001"}
    first = client.post("/v1/me/payments", json={"purpose": "BILL", "bill_id": f"{SUB}:2026-10"}, headers=hdr)
    again = client.post("/v1/me/payments", json={"purpose": "BILL", "bill_id": f"{SUB}:2026-10"}, headers=hdr)
    assert first.status_code == 201 and again.json()["order_id"] == first.json()["order_id"] and gw.orders == 1

    pid, order = first.json()["payment_id"], first.json()["order_id"]
    bad = client.post(f"/v1/me/payments/{pid}/confirm", json={"gateway_payment_id": "pay_x", "signature": "0" * 64},
                      headers=h)
    assert bad.status_code == 400

    sig = hmac.new(b"s3cret", f"{order}|pay_x".encode(), hashlib.sha256).hexdigest()
    pending = client.post(f"/v1/me/payments/{pid}/confirm", json={"gateway_payment_id": "pay_x", "signature": sig},
                          headers=h)
    assert pending.json()["status"] == "PENDING"  # gateway has not captured it yet

    gw.captured.add(order)
    ok = client.post(f"/v1/me/payments/{pid}/confirm", json={"gateway_payment_id": "pay_x", "signature": sig}, headers=h)
    assert ok.json()["status"] == "PAID"
    hook = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_x", "order_id": order}}}}
    wh = client.post("/v1/webhooks/razorpay", content=json.dumps(hook), headers={"X-Razorpay-Signature": "good"})
    assert wh.json() == {"ok": True, "settled": False}  # already settled by the browser path

    bill = client.get("/v1/me/bills/2026-10", headers=h).json()
    assert bill["status"] == "PAID" and bill["outstanding"] == 0
    from core_api.db import outbox
    from sqlalchemy import select
    with engine.connect() as cx:
        assert len(cx.execute(select(outbox)).all()) == 1  # exactly one PAYMENT_SUCCEEDED event


def test_webhook_rejects_bad_signature(env):
    client, *_ = env
    assert client.post("/v1/webhooks/razorpay", content=b"{}", headers={"X-Razorpay-Signature": "bad"}).status_code == 401


def test_simulate_from_scenario_and_upload(env):
    client, engine, state, _ = env
    issue(engine, state, "2026-10")
    h = login(client)
    assert any(s["key"] == "us-trip-2-weeks" for s in client.get("/v1/me/usage/scenarios", headers=h).json())
    r = client.post("/v1/me/usage/simulate", data={"scenario": "us-trip-2-weeks"}, headers=h).json()
    assert r["compared_to"] == "2026-10" and r["explanation"]["direction"] == "UP"
    assert r["explanation"]["drivers"][0]["category"] == "ROAMING"
    csv_body = b"usage_type,quantity,unit,roaming_zone\nDATA,2,GB,\nROAMING_DATA,500,MB,Z3\n"
    up = client.post("/v1/me/usage/simulate", files={"file": ("my.csv", csv_body, "text/csv")}, headers=h)
    assert up.status_code == 200 and any(x["code"] == "ROAMING_DATA:Z3" for x in up.json()["bill"]["lines"])
    bad = client.post("/v1/me/usage/simulate", files={"file": ("x.csv", b"usage_type,quantity,unit\nWIFI,1,GB\n")},
                      headers=h)
    assert bad.status_code == 422
    from core_api.services.bills import list_bills
    assert len(list_bills(engine, SUB)) == 1  # simulation never touched real bills/usage


def test_settlement_in_inr_keeps_bill_currency_for_the_ledger(env, monkeypatch):
    client, engine, state, gw = env
    issue(engine, state, "2026-10")
    monkeypatch.setattr(pay_router, "flag_obj", lambda *a, **k: {"currency": "INR", "fx": {"EUR": 90.0}})
    h = login(client)
    p = client.post("/v1/me/payments", json={"purpose": "BILL", "bill_id": f"{SUB}:2026-10"},
                    headers=h | {"Idempotency-Key": "inr-00000001"}).json()
    assert p["currency"] == "EUR" and p["charged_currency"] == "INR" and p["fx_rate"] == "90.0"
    assert gw.last == (p["charged_amount"], "INR") and p["charged_amount"] == p["amount"] * 90
    gw.captured.add(p["order_id"])
    sig = hmac.new(b"s3cret", f"{p['order_id']}|pay_y".encode(), hashlib.sha256).hexdigest()
    client.post(f"/v1/me/payments/{p['payment_id']}/confirm", json={"gateway_payment_id": "pay_y", "signature": sig},
                headers=h)
    bill = client.get("/v1/me/bills/2026-10", headers=h).json()
    assert bill["status"] == "PAID" and bill["amount_paid"] == bill["total"]  # ledger in EUR, not INR
