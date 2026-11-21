"""Country adapter layer for legacy order systems.

Workflows and services speak ONE shape - a TM Forum TMF622 ProductOrder - and never know which legacy
system sits below. Each country registers an adapter that translates that shape to its own system's
protocol (REST in DE, SOAP in PL here; files/MQ elsewhere) and maps the legacy states back to TMF622
states. Adding a country = adding an adapter; the app, the workflow and the other services don't change.

The legacy systems themselves are emulated (bss_orders table + the legacy_bss loop in the worker), with
realistic latency and rejections; the adapters are the real seam.
"""
from __future__ import annotations

import json
import uuid
from typing import Protocol
from xml.sax.saxutils import escape

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

BASE = "/tmf-api/productOrderingManagement/v4/productOrder"  # TMF622 resource path

# legacy status -> TMF622 ProductOrder.state
TMF_STATE = {"SUBMITTED": "acknowledged", "PROCESSING": "inProgress", "CONFIRMED": "completed",
             "REJECTED": "failed", "UNKNOWN": "pending"}


def product_order(order_id: str, subscriber_id: str, offering: str, country: str, action: str = "add") -> dict:
    """TMF622 ProductOrder (subset) - the only order shape services exchange.
    action: "add" = buy an add-on; "modify" = switch the tariff to `offering`."""
    return {"@type": "ProductOrder", "externalId": order_id, "category": "addon" if action == "add" else "tariff",
            "requestedCompletionDate": None,
            "relatedParty": [{"id": subscriber_id, "role": "customer", "@referredType": "Individual"}],
            "productOrderItem": [{"id": "1", "action": action,
                                  "productOffering": {"id": offering, "@referredType": "ProductOffering"}}],
            "@baseType": "ProductOrder", "place": [{"id": country, "role": "market"}]}


def market(order: dict) -> str:
    return next(p["id"] for p in order.get("place", []) if p.get("role") == "market")


class OrderAdapter(Protocol):
    protocol: str
    prefix: str

    def submit(self, engine: Engine, order: dict) -> str: ...
    def state(self, engine: Engine, external_id: str) -> str: ...


class _LegacyEmulator:
    """Shared emulator storage: what the legacy system persists on its side."""

    prefix = "BSS"

    def _store(self, engine: Engine, order: dict, wire: str) -> str:
        from ..db import bss_orders

        oid = order["externalId"]
        with engine.begin() as cx:
            existing = cx.execute(select(bss_orders.c.id).where(bss_orders.c.order_id == oid)).scalar()
            if existing:
                return existing  # idempotent: one legacy order per order
            bid = f"{self.prefix}-{uuid.uuid4().hex[:10].upper()}"
            cx.execute(insert(bss_orders).values(id=bid, order_id=oid, status="SUBMITTED"))
        self.last_wire = wire
        return bid

    def state(self, engine: Engine, external_id: str) -> str:
        from ..db import bss_orders

        with engine.connect() as cx:
            legacy = cx.execute(select(bss_orders.c.status).where(bss_orders.c.id == external_id)).scalar()
        return TMF_STATE.get(legacy or "UNKNOWN", "pending")


class RestBssAdapter(_LegacyEmulator):
    """DE: legacy REST/JSON order API with its own field names."""

    protocol, prefix = "rest", "DEB"

    def submit(self, engine: Engine, order: dict) -> str:
        item = order["productOrderItem"][0]
        wire = json.dumps({"auftragsnummer": order["externalId"], "kunde": order["relatedParty"][0]["id"],
                           "option": item["productOffering"]["id"],
                           "aktion": "BUCHEN" if item["action"] == "add" else "TARIFWECHSEL"})
        return self._store(engine, order, wire)


class SoapBssAdapter(_LegacyEmulator):
    """PL: legacy SOAP order service."""

    protocol, prefix = "soap", "PLB"

    def submit(self, engine: Engine, order: dict) -> str:
        item = order["productOrderItem"][0]["productOffering"]["id"]
        op = "CreateOrder" if order["productOrderItem"][0]["action"] == "add" else "ChangeTariff"
        wire = ('<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ord="urn:legacy:orders">'
                f'<soapenv:Body><ord:{op}><ord:Ref>{escape(order["externalId"])}</ord:Ref>'
                f'<ord:Msisdn>{escape(order["relatedParty"][0]["id"])}</ord:Msisdn><ord:Option>{escape(item)}</ord:Option>'
                f'</ord:{op}></soapenv:Body></soapenv:Envelope>')
        return self._store(engine, order, wire)


ADAPTERS: dict[str, OrderAdapter] = {"DE": RestBssAdapter(), "PL": SoapBssAdapter()}


def adapter_for_id(external_id: str) -> OrderAdapter | None:
    return next((a for a in ADAPTERS.values() if external_id.startswith(a.prefix + "-")), None)


def for_country(country: str) -> OrderAdapter:
    try:
        return ADAPTERS[country]
    except KeyError as e:
        raise LookupError(f"no order adapter registered for market {country!r}") from e
