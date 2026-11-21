"""Post-deploy smoke test (`helm test`): exercises every case-study flow against the live stack.

Runs inside the cluster with the demo password mounted from the Vault-synced secret, and prints
only pass/fail per check - no credentials or customer data in the output.
"""
from __future__ import annotations

import os
import sys

import httpx

API = os.environ.get("SMOKE_API", "http://core-api:8080")
PAYMENTS = os.environ.get("SMOKE_PAYMENTS", "http://payments-service:8080")
ASSISTANT = os.environ.get("SMOKE_ASSISTANT", "http://assistant-service:8080")
WEB = os.environ.get("SMOKE_WEB", "http://web")
ADAPTER = os.environ.get("SMOKE_ADAPTER", "http://order-adapter:8080")
SUB = os.environ.get("SMOKE_SUBSCRIBER", "4915100000002")


def main() -> None:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, info: str = "") -> None:
        results.append((name, ok, info))

    c = httpx.Client(base_url=API, timeout=30)
    r = c.post("/v1/auth/login", json={"subscriber_id": SUB, "password": os.environ["APP_DEMO_PASSWORD"]})
    check("login", r.status_code == 200, str(r.status_code))
    h = {"Authorization": f"Bearer {r.json().get('access_token', '')}"}

    home = c.get("/v1/me/home", headers=h)
    hj = home.json() if home.status_code == 200 else {}
    check("home snapshot", home.status_code == 200 and home.headers.get("cache-control") == "private, no-store")
    check("12-month trend", len(hj.get("trend", [])) >= 11, f"{len(hj.get('trend', []))} bills")
    check("unbilled amount", hj.get("unbilled", {}).get("total", 0) > 0, str(hj.get("unbilled", {}).get("total")))
    check("bill pending", hj.get("outstanding", {}).get("amount", 0) > 0)
    last = (hj.get("last_bill") or {}).get("cycle", "")
    check("view bill", c.get(f"/v1/me/bills/{last}", headers=h).status_code == 200, last)
    pdf = c.get(f"/v1/me/bills/{last}/pdf", headers=h)
    check("download bill PDF", pdf.status_code == 200 and pdf.content[:4] == b"%PDF", f"{len(pdf.content)} bytes")
    ex = c.get(f"/v1/me/bills/{last}/explanation?ai=false", headers=h).json()
    e = ex.get("explanation", {})
    check("why bill changed", e.get("direction") in ("UP", "DOWN", "SAME") and bool(e.get("facts")), e.get("direction", ""))
    check("top usage drivers", isinstance(e.get("top_usage_drivers"), list))
    ai = c.get(f"/v1/me/bills/{last}/explanation?lang=de", headers=h, timeout=90)
    aj = ai.json() if ai.status_code == 200 else {}
    check("LLM explanation (verified, cited)", aj.get("status") in ("VERIFIED", "REPAIRED") and bool((aj.get("llm") or {}).get("summary")),
          aj.get("status", ""))
    ask = httpx.post(f"{ASSISTANT}/v1/me/assistant/ask", headers=h, json={"question": "Have I paid my latest bill?"}, timeout=120)
    check("assistant (tools via broker)", ask.status_code == 200 and ask.json().get("status") in ("VERIFIED", "REPAIRED"),
          f"{ask.json().get('status')} · tools {ask.json().get('trace', {}).get('totals', {}).get('tool_calls')}")
    plans = c.get("/v1/me/orders/plans/available", headers=h)
    pj = plans.json() if plans.status_code == 200 else {}
    target = next((x["code"] for x in pj.get("plans", []) if x["code"] != pj.get("current")), None)
    prop = c.post("/v1/me/orders/plan", headers=h, json={"plan_code": target}) if target else None
    ok = prop is not None and prop.status_code == 201
    if ok:  # leave the customer's tariff untouched: decline the proposal
        c.post(f"/v1/me/orders/{prop.json()['order_id']}/decline", headers=h)
    check("tariff change proposal (order-events)", ok, f"{getattr(prop, 'status_code', '-')} -> {target}")
    tmf = httpx.get(f"{ADAPTER}/tmf-api/productOrderingManagement/v4/productOrder/DEB-NOTEXIST", timeout=10)
    check("order adapter (TMF622)", tmf.status_code == 404, str(tmf.status_code))
    sim = c.post("/v1/me/usage/simulate", data={"scenario": "us-trip-2-weeks"}, headers=h)
    check("select usage scenario", sim.status_code == 200 and sim.json()["bill"]["total"] > 0)
    up = c.post("/v1/me/usage/simulate", headers=h, files={"file": (
        "u.csv", b"usage_type,quantity,unit,roaming_zone\nDATA,4,GB,\nROAMING_DATA,300,MB,Z3\n", "text/csv")})
    check("upload usage file", up.status_code == 200)
    other = c.get("/v1/me/bills/2026-10", headers={"Authorization": "Bearer nope"})
    check("auth enforced", other.status_code == 401)
    pay = httpx.post(f"{PAYMENTS}/v1/me/payments", headers=h | {"Idempotency-Key": "smoke-test-0001"}, timeout=30,
                    json={"purpose": "BILL", "bill_id": hj.get("outstanding", {}).get("bill_id")})
    check("payment order (Razorpay)", pay.status_code in (200, 201), f"{pay.status_code} {pay.text[:80]}")
    w = httpx.get(f"{WEB}/login", timeout=15)
    check("web SSR login page", w.status_code == 200 and "Sign in" in w.text and "Mobile number" in w.text)

    width = max(len(n) for n, *_ in results)
    for name, ok, info in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}  {info}")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
