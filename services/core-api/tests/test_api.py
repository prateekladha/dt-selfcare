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
from core_api.services.billing_ledger import drain_outbox_into_billing
from core_api.services.gateway import GatewayOrder
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from telco_common.catalog import MB
from telco_common.demo import DEMO_SUBSCRIBERS
from telco_common.models import Country, UsageRecord, UsageType
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState

N_DEMO = len(DEMO_SUBSCRIBERS)
SUB, OTHER = "4915100000002", "4915100000001"  # traveller (DE-FLEX), streamer (DE-M)


class FakeGateway:
    name, key_id, secret = "razorpay", "rzp_test_x", "s3cret"

    def __init__(self):
        self.orders, self.captured, self.failed, self.authorized, self.status_calls = 0, set(), set(), set(), 0

    def order_status(self, order_id):
        from core_api.services.gateway import GatewayStatus
        self.status_calls += 1
        if order_id in self.captured:
            return GatewayStatus("CAPTURED", f"pay_{order_id}", "paid", [{"status": "captured"}])
        if order_id in self.authorized:
            return GatewayStatus("AUTHORIZED", f"pay_{order_id}", "attempted", [{"status": "authorized"}])
        if order_id in self.failed:
            return GatewayStatus("FAILED", f"pay_{order_id}", "attempted", [{"status": "failed"}])
        return GatewayStatus("NO_ATTEMPT", None, "created", [])

    def capture(self, payment_id, amount, currency):
        self.captured.add(payment_id.removeprefix("pay_"))
        return True

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
    import core_api.routers.demo as demo_mod
    import core_api.routers.simulate as sim_mod
    monkeypatch.setattr(demo_mod, "get_usage_state", lambda: state)
    monkeypatch.setattr(sim_mod, "get_usage_state", lambda: state)
    import core_api.main as main_mod
    monkeypatch.setattr(main_mod, "get_usage_state", lambda: state)
    if not getattr(main_mod.app, "_test_services_mounted", False):  # payments + assistant are separate services
        from core_api.routers import assistant as assistant_router
        main_mod.app.include_router(pay_router.router)
        main_mod.app.include_router(assistant_router.router)
        main_mod.app._test_services_mounted = True
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


def test_failed_logins_are_limited_per_client_ip_across_accounts(env):
    """Password spraying: one password against many accounts from one IP stops after 20 failures,
    while other IPs (and the per-account limit) are unaffected; successes don't count."""
    client, *_ = env
    spray = {"X-Client-IP": "203.0.113.7"}
    for i in range(20):
        r = client.post("/v1/auth/login", headers=spray, json={"subscriber_id": f"49151999{i:04d}", "password": "Summer2026"})
        assert r.status_code == 401
    r = client.post("/v1/auth/login", headers=spray, json={"subscriber_id": SUB, "password": "demo-pass-123"})
    assert r.status_code == 429  # blocked even with the right password: the IP is cooling down
    ok = client.post("/v1/auth/login", headers={"X-Client-IP": "198.51.100.9"}, json={"subscriber_id": SUB, "password": "demo-pass-123"})
    assert ok.status_code == 200


def test_ipv6_clients_are_limited_per_64_and_garbage_ip_is_ignored():
    from core_api.routers.auth import client_net
    assert client_net("2001:db8:1:2::1") == client_net("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert client_net("203.0.113.7") == "203.0.113.7"
    assert client_net("not-an-ip") is None and client_net(None) is None


def test_tokens_are_rs256_and_a_verifier_only_service_cannot_issue_or_accept_forgeries(monkeypatch):
    import jwt as pyjwt
    from core_api import security
    from core_api.settings import get_settings

    _, pub = security._dev_keypair()
    token, _ = security.issue_token("4915100000001", "DE")
    assert pyjwt.get_unverified_header(token)["alg"] == "RS256"
    # an HS256 token (the old shared-secret scheme, or an alg-confusion attempt) is rejected outright
    forged = pyjwt.encode({"sub": "4915100000002", "country": "DE", "iss": security.ISSUER, "exp": 9_999_999_999},
                          "any-shared-secret", algorithm="HS256")
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        security.decode_token(forged)
    # a service configured with only the public key verifies tokens but refuses to sign them
    monkeypatch.setenv("APP_JWT_PUBLIC_KEY", pub)
    get_settings.cache_clear()
    try:
        assert security.decode_token(token).subscriber_id == "4915100000001"
        with pytest.raises(RuntimeError):
            security.issue_token("4915100000002", "DE")
        monkeypatch.setenv("APP_REQUIRE_JWT_KEYS", "true")
        monkeypatch.delenv("APP_JWT_PUBLIC_KEY")
        get_settings.cache_clear()
        with pytest.raises(RuntimeError):  # misconfigured cluster fails loudly, never a throwaway key
            security.decode_token(token)
    finally:
        get_settings.cache_clear()


def test_home_snapshot_bills_explanation_and_pdf(env):
    client, engine, state, _ = env
    add_usage(state, SUB, datetime(2026, 9, 10, tzinfo=UTC), [(UsageType.DATA, None, 1_000 * MB)])
    add_usage(state, SUB, datetime(2026, 10, 10, tzinfo=UTC), [(UsageType.DATA, None, 2_000 * MB),
                                                               (UsageType.ROAMING_DATA, "Z2", 500 * MB)])
    add_usage(state, SUB, datetime.now(UTC), [(UsageType.DATA, None, 2_600 * MB)])
    assert issue(engine, state, "2026-09", settle_before="2026-10") == {"created": N_DEMO, "skipped_existing": 0}
    assert issue(engine, state, "2026-10")["created"] == N_DEMO
    assert issue(engine, state, "2026-10")["skipped_existing"] == N_DEMO  # bill run is idempotent

    h = login(client)
    home = client.get("/v1/me/home", headers=h)
    assert home.status_code == 200 and home.headers["cache-control"] == "private, no-store"
    body = home.json()
    assert body["usage"]["data_pct"] > 80 and body["unbilled"]["currency"] == "EUR"
    assert [b["cycle"] for b in body["trend"]] == ["2026-09", "2026-10"]
    assert body["trend"][0]["status"] == "PAID" and body["outstanding"]["bill_id"] == f"{SUB}:2026-10"

    ex = client.get("/v1/me/bills/2026-10/explanation", headers=h).json()
    assert ex["status"] == "UNAVAILABLE" and ex["llm"] is None  # AI flag off: no canned text, breakdown only
    assert ex["explanation"]["drivers"][0]["code"] == "ROAMING_DATA:Z2"
    assert "increased" in ex["explanation"]["facts"][0]

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
    assert pending.json()["status"] != "PAID"  # gateway has not captured it yet: the browser alone can't settle

    gw.captured.add(order)
    ok = client.post(f"/v1/me/payments/{pid}/confirm", json={"gateway_payment_id": "pay_x", "signature": sig}, headers=h)
    assert ok.json()["status"] == "PAID"
    hook = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_x", "order_id": order}}}}
    wh = client.post("/v1/webhooks/razorpay", content=json.dumps(hook), headers={"X-Razorpay-Signature": "good"})
    assert wh.json()["settled"] is False and wh.json()["result"] == "already_paid"  # settled by the browser path

    assert drain_outbox_into_billing(engine) == 1  # payment-events -> billing (exactly once)
    assert drain_outbox_into_billing(engine) == 0  # redelivery is a no-op
    bill = client.get("/v1/me/bills/2026-10", headers=h).json()
    assert bill["status"] == "PAID" and bill["outstanding"] == 0
    from core_api.db import outbox
    from sqlalchemy import select
    with engine.connect() as cx:
        assert len(cx.execute(select(outbox)).all()) == 1  # exactly one PAYMENT_SUCCEEDED event


def test_second_tab_gets_the_open_order_so_a_bill_cant_be_paid_twice(env):
    client, engine, state, gw = env
    issue(engine, state, "2026-10")
    h = login(client)
    body = {"purpose": "BILL", "bill_id": f"{SUB}:2026-10"}
    a = client.post("/v1/me/payments", json=body, headers=h | {"Idempotency-Key": "tab-a-0000001"}).json()
    b = client.post("/v1/me/payments", json=body, headers=h | {"Idempotency-Key": "tab-b-0000001"}).json()
    assert a["order_id"] == b["order_id"] and gw.orders == 1


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
    assert r["baseline"] == "your-last-cycle"  # scenario is applied on top of the real last-cycle usage
    import re
    assert re.fullmatch(r"\d{4}-\d{2}", r["bill"]["cycle"])  # must fit VARCHAR2(7) on Oracle
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
    drain_outbox_into_billing(engine)
    bill = client.get("/v1/me/bills/2026-10", headers=h).json()
    assert bill["status"] == "PAID" and bill["amount_paid"] == bill["total"]  # ledger in EUR, not INR


def test_system_overview_shows_every_layer_and_only_my_data(env, monkeypatch):
    client, engine, state, _ = env
    from core_api.routers import system
    monkeypatch.setattr(system, "_stream", lambda: {"engine": "stub", "partitions": []})
    monkeypatch.setattr(system, "get_usage_state", lambda: state)
    monkeypatch.setattr(system, "get_valkey", lambda: state.r)
    monkeypatch.setattr(system, "_cache", {})
    monkeypatch.setattr(system, "_valkey", lambda: {"engine": "stub", "keys": state.r.dbsize()})  # fakeredis: no INFO
    add_usage(state, SUB, datetime.now(UTC), [(UsageType.DATA, None, 500 * MB)])
    issue(engine, state, "2026-10")
    body = client.get("/v1/system/overview", headers=login(client)).json()
    assert body["database"]["tables"]["bills"] == N_DEMO and body["valkey"]["keys"] > 0
    assert body["you"]["valkey_usage_hash"]["DATA"] == str(500 * MB)
    assert all(b["id"].startswith(SUB) for b in body["you"]["db_bills"])
    assert "ai-provider" in body["flags"]


def test_razorpay_adapter_captures_authorised_payment():
    import httpx as _httpx
    from core_api.services.gateway import Razorpay
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/payments") and req.method == "GET":
            return _httpx.Response(200, json={"items": [{"id": "pay_1", "status": "authorized", "amount": 100, "currency": "INR"}]})
        if req.url.path.endswith("/capture"):
            return _httpx.Response(200, json={"id": "pay_1", "status": "captured"})
        return _httpx.Response(404)

    gw = Razorpay("rzp_test_x", "s", "w", http=_httpx.Client(transport=_httpx.MockTransport(handler)))
    assert gw.is_order_paid("order_1") == (True, "pay_1")
    assert ("POST", "/v1/payments/pay_1/capture") in calls



def test_demo_panel_is_computed_from_real_bills_and_off_by_default(env, monkeypatch):
    client, engine, state, _ = env
    assert client.get("/v1/demo/accounts").status_code == 404  # safe default: flag off
    import core_api.routers.demo as demo_mod
    monkeypatch.setattr(demo_mod, "flag_bool", lambda *a, **k: True)
    add_usage(state, SUB, datetime(2026, 9, 10, tzinfo=UTC), [(UsageType.DATA, None, 1_000 * MB)])
    add_usage(state, SUB, datetime(2026, 10, 10, tzinfo=UTC), [(UsageType.ROAMING_DATA, "Z2", 500 * MB)])
    issue(engine, state, "2026-09")
    issue(engine, state, "2026-10")
    jonas = next(a for a in client.get("/v1/demo/accounts").json() if a["subscriber_id"] == SUB)
    assert jonas["headline"].startswith("October bill +") and "mainly roaming data in zone Z2" in jonas["headline"]


def test_late_cdrs_after_bill_issue_become_an_adjustment_on_the_next_bill(env):
    _client, engine, state, _ = env
    from core_api.services.bills import close_cycle, get_bill_row, row_to_bill
    add_usage(state, SUB, datetime(2026, 9, 10, tzinfo=UTC), [(UsageType.DATA, None, 1_000 * MB)])
    close_cycle(engine, state, "2026-09")
    sept_total = get_bill_row(engine, SUB, "2026-09").total
    # roaming CDR for September arrives in October, after the September bill was issued
    add_usage(state, SUB, datetime(2026, 9, 28, tzinfo=UTC), [(UsageType.ROAMING_DATA, "Z2", 100 * MB)])
    close_cycle(engine, state, "2026-10")
    assert get_bill_row(engine, SUB, "2026-09").total == sept_total  # issued bill is immutable
    oct_lines = {x.code: x for x in row_to_bill(get_bill_row(engine, SUB, "2026-10")).lines}
    assert oct_lines["ADJ:2026-09"].amount == 199  # 100 MB * 1.99 cents, net
    add_usage(state, SUB, datetime(2026, 11, 1, tzinfo=UTC), [(UsageType.DATA, None, 1)])



def _pay(client, h, key):
    return client.post("/v1/me/payments", json={"purpose": "BILL", "bill_id": f"{SUB}:2026-10"},
                       headers=h | {"Idempotency-Key": key}).json()


def test_out_of_order_failed_webhook_after_capture_cannot_undo_paid(env):
    client, engine, state, gw = env
    issue(engine, state, "2026-10")
    h = login(client)
    p = _pay(client, h, "ooo-00000001")
    gw.captured.add(p["order_id"])
    ok = {"event": "payment.captured", "payload": {"payment": {"entity": {"id": "pay_1", "order_id": p["order_id"],
                                                                          "status": "captured"}}}}
    assert client.post("/v1/webhooks/razorpay", content=json.dumps(ok), headers={"X-Razorpay-Signature": "good"}).json()["settled"]
    # a 'payment.failed' for a dropped attempt arrives LATER; its payload is not trusted
    late = {"event": "payment.failed", "payload": {"payment": {"entity": {"id": "pay_0", "order_id": p["order_id"],
                                                                         "status": "failed"}}}}
    r = client.post("/v1/webhooks/razorpay", content=json.dumps(late), headers={"X-Razorpay-Signature": "good"}).json()
    assert r["result"] == "already_paid"
    drain_outbox_into_billing(engine)
    assert client.get("/v1/me/bills/2026-10", headers=h).json()["status"] == "PAID"


def test_failed_attempt_then_successful_retry_on_same_order_ends_paid(env):
    client, engine, state, gw = env
    issue(engine, state, "2026-10")
    h = login(client)
    p = _pay(client, h, "retry-0000001")
    from core_api.services.payments import reconcile_order
    gw.failed.add(p["order_id"])
    assert reconcile_order(engine, gw, p["order_id"], "webhook")["result"] == "failed"
    gw.failed.discard(p["order_id"])
    gw.authorized.add(p["order_id"])  # customer retried; authorised but not yet captured -> we capture
    assert reconcile_order(engine, gw, p["order_id"], "reconciler")["result"] == "paid"


def test_reconciler_catches_up_after_downtime_and_expires_abandoned_orders(env):
    from datetime import timedelta

    from core_api.services.payments import due_for_check, next_delay_s, reconcile_order
    client, engine, state, gw = env
    issue(engine, state, "2026-10")
    h = login(client)
    paid_later = _pay(client, h, "down-0000001")
    abandoned = client.post("/v1/me/payments", json={"purpose": "ADVANCE"}, headers=h | {"Idempotency-Key": "down-0000002"}).json()
    gw.captured.add(paid_later["order_id"])  # captured while our system was down (no webhook received)
    now = datetime.now(UTC) + timedelta(hours=2)
    due = due_for_check(engine, now)
    assert paid_later["order_id"] in due and abandoned["order_id"] in due
    for oid in due:
        reconcile_order(engine, gw, oid, "reconciler", now=now)
    drain_outbox_into_billing(engine)
    assert client.get("/v1/me/bills/2026-10", headers=h).json()["status"] == "PAID"
    later = datetime.now(UTC) + timedelta(hours=30)
    assert reconcile_order(engine, gw, abandoned["order_id"], "reconciler", now=later)["result"] == "expired"
    assert abandoned["order_id"] not in due_for_check(engine, later + timedelta(days=1))
    assert [next_delay_s(n) for n in range(4)] == [30, 60, 120, 240]
    assert [next_delay_s(n, "polynomial") for n in range(4)] == [30, 60, 130, 240]
    assert next_delay_s(20) == 3600  # capped


def test_refresh_token_rotates_survives_parallel_use_and_revokes(env):
    client, *_ = env
    r = client.post("/v1/auth/login", json={"subscriber_id": SUB, "password": "demo-pass-123"}).json()
    rt = r["refresh_token"]
    a = client.post("/v1/auth/refresh", json={"refresh_token": rt})
    b = client.post("/v1/auth/refresh", json={"refresh_token": rt})  # parallel request within the grace window
    assert a.status_code == b.status_code == 200 and a.json()["refresh_token"] == b.json()["refresh_token"]
    new = a.json()
    assert client.get("/v1/me/home", headers={"Authorization": f"Bearer {new['access_token']}"}).status_code == 200
    client.post("/v1/auth/logout", json={"refresh_token": new["refresh_token"]})
    assert client.post("/v1/auth/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 401


def test_home_read_model_cached_and_invalidated_by_events(env):
    """Home = one aggregate call; 2nd read is served from cache; a payment event invalidates it."""
    client, engine, state, gw = env
    issue(engine, state, "2026-10")
    h = login(client)
    first = client.get("/v1/me/home", headers=h)
    second = client.get("/v1/me/home", headers=h)
    assert first.headers["x-home-cache"] == "miss" and second.headers["x-home-cache"] == "hit"
    assert second.json()["outstanding"]["bill_id"] == f"{SUB}:2026-10"
    p = _pay(client, h, "cache-0000001")
    gw.captured.add(p["order_id"])
    from core_api.services.payments import reconcile_order
    reconcile_order(engine, gw, p["order_id"], "webhook")
    drain_outbox_into_billing(engine)  # billing applies PAYMENT_SUCCEEDED -> bumps the home version
    after = client.get("/v1/me/home", headers=h)
    assert after.headers["x-home-cache"] == "miss" and after.json()["outstanding"]["bill_id"] is None
    assert client.get("/v1/me/bills/latest/explanation?ai=false", headers=h).json()["bill_id"] == f"{SUB}:2026-10"


def test_daily_usage_endpoint_adds_up_to_totals(env):
    client, _engine, state, _ = env
    now = datetime.now(UTC)
    add_usage(state, SUB, now, [(UsageType.DATA, None, 1_200 * MB), (UsageType.ROAMING_DATA, "Z2", 300 * MB)])
    h = login(client)
    j = client.get("/v1/me/usage/daily", headers=h).json()
    assert j["current"] and j["through_day"] == now.day and len(j["days"]) == now.day
    today = j["days"][-1]
    assert today["data_mb"] >= 1_500 and today["roaming_mb"] >= 300
    assert j["totals"]["data_mb"] == sum(d["data_mb"] for d in j["days"])
    assert client.get("/v1/me/usage/daily?cycle=2999-01", headers=h).status_code == 422
