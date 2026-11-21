"""Payment gateway port + Razorpay adapter (real API, test mode keys).

The domain talks to `PaymentGateway`; adding Cashfree / Adyen / Stripe is one more adapter and a
flag value (`payment-gateway`), routed per country without touching the payment domain.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Protocol

import httpx

from .resilience import CircuitBreaker


@dataclass(frozen=True)
class GatewayOrder:
    order_id: str
    amount: int
    currency: str
    checkout: dict  # what the browser needs to open the gateway's checkout


class PaymentGateway(Protocol):
    name: str

    def create_order(self, amount: int, currency: str, receipt: str, notes: dict) -> GatewayOrder: ...
    def verify_client_signature(self, order_id: str, payment_id: str, signature: str) -> bool: ...
    def verify_webhook(self, body: bytes, signature: str) -> bool: ...
    def is_order_paid(self, order_id: str) -> tuple[bool, str | None]: ...


class Razorpay:
    name = "razorpay"
    BASE = "https://api.razorpay.com/v1"

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str, http: httpx.Client | None = None):
        if not key_id or not key_secret:
            raise RuntimeError("Razorpay keys are not configured")
        self.key_id, self._secret, self._wh = key_id, key_secret, webhook_secret
        self.http = http or httpx.Client(timeout=10, auth=(key_id, key_secret))
        self.breaker = CircuitBreaker("razorpay", failures=5, reset_s=30)

    def _call(self, method: str, path: str, **kw) -> dict:
        self.breaker.before()
        try:
            r = self.http.request(method, f"{self.BASE}{path}", **kw)
        except httpx.HTTPError:
            self.breaker.failure()
            raise
        if r.status_code >= 500:
            self.breaker.failure()
            r.raise_for_status()
        self.breaker.success()
        if r.status_code >= 400:
            raise ValueError(f"razorpay rejected request: {r.status_code} {r.text[:300]}")
        return r.json()

    def create_order(self, amount: int, currency: str, receipt: str, notes: dict) -> GatewayOrder:
        o = self._call("POST", "/orders", json={"amount": amount, "currency": currency, "receipt": receipt[:40],
                                                 "notes": notes})
        return GatewayOrder(o["id"], o["amount"], o["currency"], {"provider": "razorpay", "key_id": self.key_id,
                                                                  "order_id": o["id"]})

    def verify_client_signature(self, order_id: str, payment_id: str, signature: str) -> bool:
        expected = hmac.new(self._secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def verify_webhook(self, body: bytes, signature: str) -> bool:
        if not self._wh:
            return False
        expected = hmac.new(self._wh.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)

    def is_order_paid(self, order_id: str) -> tuple[bool, str | None]:
        """Server-side confirmation: never trust the browser's 'success' alone."""
        items = self._call("GET", f"/orders/{order_id}/payments").get("items", [])
        for p in items:
            if p.get("status") == "captured":
                return True, p["id"]
        return False, None
