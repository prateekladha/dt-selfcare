"""Layer 1 - durable context spine.

Every fact the assistant may use is materialised as a small, citable document in Autonomous DB, built
ONLY from systems of record (never typed by hand):
  per subscriber: plan profile · each issued bill · each bill-to-bill change · live current-cycle usage
                  (from the CDR running totals in Valkey, versioned by stream offset) · alerts · payments
  shared:         tariff/plan sheets, roaming zones, rounding, VAT, add-ons, billing-cycle and late-CDR
                  policy - generated from the product catalogue

Documents are upserted by content hash, so a refresh after new CDRs only re-embeds what changed.
No names, phone numbers or emails are written into documents (data minimisation for the LLM).
"""
from __future__ import annotations

import array
import hashlib
import json
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.engine import Engine
from telco_common.billing import data_usage_pct
from telco_common.catalog import ADDONS, COUNTRIES, PLANS, ROAM_LIKE_AT_HOME_ZONE, get_plan
from telco_common.explainer import explain, fmt_money
from telco_common.models import cycle_of
from telco_common.partitioning import partition_for
from telco_common.usage_state import UsageState

from ..db import context_docs, notifications, payments, subscribers
from ..services.bills import list_bills, row_to_bill, unbilled
from .embed import Embedder


@dataclass(frozen=True)
class Doc:
    id: str
    subscriber_id: str | None
    doc_type: str
    cycle: str | None
    title: str
    body: str
    source_ref: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(f"{self.title}\n{self.body}".encode()).hexdigest()


def _money(minor: int, cur: str) -> str:
    return fmt_money(minor, cur)


def _month(cycle: str) -> str:
    return datetime(int(cycle[:4]), int(cycle[5:]), 1).strftime("%B %Y")


# ---------------- shared knowledge (from the product catalogue) ----------------
def global_docs() -> list[Doc]:
    docs: list[Doc] = []
    for p in PLANS.values():
        cur = COUNTRIES[p.country].currency
        voice = "unlimited minutes" if p.voice_min is None else f"{p.voice_min} minutes"
        sms = "unlimited SMS" if p.sms is None else f"{p.sms} SMS"
        over = ("Data beyond the allowance is not charged; speed is reduced (throttled) until the next cycle."
                if p.overage_policy == "THROTTLE" else
                f"Data beyond the allowance costs {p.data_overage_per_mb} {cur} cents per started MB.")
        roam = "; ".join(f"zone {z}: {r.data_per_mb} cents/MB data, {r.voice_per_min} cents/min calls"
                         for z, r in p.roaming.items())
        docs.append(Doc(f"global:tariff:{p.code}", None, "tariff", None, f"Plan {p.name} ({p.code}, {p.country})",
                        f"Plan {p.name} ({p.code}) in {p.country} costs {_money(p.monthly_fee, cur)} per month before VAT. "
                        f"It includes {p.data_mb:,} MB of data, {voice} and {sms}. {over} "
                        f"Extra minutes cost {p.voice_overage_per_min} cents each and extra SMS {p.sms_each} cents. "
                        f"International calls from home cost {p.intl_voice_per_min} cents per minute. "
                        f"Roaming outside the EU: {roam}.", f"catalog:plan:{p.code}"))
    docs.append(Doc("global:policy:roaming", None, "policy", None, "Roaming zones and EU roam-like-at-home",
                    f"Roaming in zone {ROAM_LIKE_AT_HOME_ZONE} (EU/EEA) is 'roam like at home': it uses the domestic "
                    "allowance and costs nothing extra. Zone Z2 (for example Switzerland, the UK and the US) and zone "
                    "Z3 (rest of the world) are charged per MB of data and per minute of calls at the plan's roaming "
                    "rates. A roaming pack or data add-on can reduce the cost of travel.", "catalog:roaming"))
    docs.append(Doc("global:policy:rounding", None, "policy", None, "How usage is rounded on the bill",
                    "Usage is summed for the whole billing cycle from the call detail records (CDRs). Data is billed "
                    "per started MB (1 MB = 1,000,000 bytes) and calls per started minute, rounded up once per usage "
                    "type per cycle. For example 371.04 MB is billed as 372 MB.", "rating-engine:rounding"))
    vat = "; ".join(f"{c.value}: {int(cfg.vat_rate * 100)}% VAT, prices in {cfg.currency}" for c, cfg in COUNTRIES.items())
    docs.append(Doc("global:policy:vat", None, "policy", None, "VAT and currency per country",
                    f"Bills add VAT on top of the net amounts. {vat}.", "catalog:countries"))
    adds = "; ".join(f"{a.name} ({a.code}, {a.country}): {a.data_mb:,} MB for {_money(a.price, COUNTRIES[a.country].currency)}"
                     for a in ADDONS.values())
    docs.append(Doc("global:policy:addons", None, "policy", None, "Data add-ons",
                    f"Data add-ons extend the current cycle's data allowance and are billed once: {adds}. Buying an "
                    "add-on re-arms the 50/80/100% data alerts for the bigger allowance.", "catalog:addons"))
    docs.append(Doc("global:policy:cycle", None, "policy", None, "Billing cycle, alerts, late usage and payments",
                    "A billing cycle is one calendar month (UTC, by the event time of each CDR). The bill is issued on "
                    "the 1st of the next month and is due 14 days later. Data alerts are sent once per cycle at 50%, "
                    "80% and 100% of the allowance. Usage that arrives after a bill was issued is not added to that "
                    "bill; it is corrected on the next bill. Payments made during a cycle in advance are credited on "
                    "the next bill. A bill is marked paid only after the payment gateway confirms the capture.",
                    "billing-policy"))
    return docs


# ---------------- per-subscriber documents (from systems of record) ----------------
def subscriber_docs(engine: Engine, state: UsageState, sub_id: str, now: datetime | None = None) -> list[Doc]:
    now = now or datetime.now(UTC)
    with engine.connect() as cx:
        sub = cx.execute(select(subscribers).where(subscribers.c.id == sub_id)).one()
        pays = cx.execute(select(payments).where(payments.c.subscriber_id == sub_id)
                          .order_by(payments.c.created_at.desc()).limit(10)).all()
        notes = cx.execute(select(notifications).where(notifications.c.subscriber_id == sub_id)
                           .order_by(notifications.c.created_at.desc()).limit(20)).all()
    plan = get_plan(sub.plan_code)
    cur = COUNTRIES[plan.country].currency
    docs = [Doc(f"{sub_id}:profile", sub_id, "profile", None, "Your plan",
                f"The customer is on plan {plan.name} ({plan.code}) in {plan.country}, billed in {cur}. "
                f"Monthly fee {_money(plan.monthly_fee, cur)} before VAT, {plan.data_mb:,} MB data included, "
                f"overage policy {plan.overage_policy}.", f"crm:subscriber:{sub_id}")]

    rows = list_bills(engine, sub_id, 13)
    prev = None
    for row in rows:
        b = row_to_bill(row)
        lines = "; ".join(f"{x.label} {x.quantity:,} {x.unit} = {_money(x.amount, cur)}" for x in b.lines)
        u = b.usage
        roam = ", ".join(f"zone {z} {mb:,} MB" for z, mb in u.roaming_data_mb.items()) or "none"
        docs.append(Doc(f"{sub_id}:bill:{b.cycle}", sub_id, "bill", b.cycle, f"Bill for {_month(b.cycle)}",
                        f"The {_month(b.cycle)} bill totals {_money(b.total, cur)} including {int(float(b.vat_rate) * 100)}% "
                        f"VAT ({_money(b.vat, cur)}); net {_money(b.subtotal, cur)}. "
                        + ("Paid in full. " if row.amount_paid >= row.total else
                           f"Unpaid: {_money(row.total - row.amount_paid, cur)} outstanding"
                           + (f" ({_money(row.amount_paid, cur)} paid so far)" if row.amount_paid else "") + ". ")
                        + f"Due date {_day(row.due_date)}. Lines: {lines}. Usage from the network usage records: "
                        f"{u.data_mb:,} MB of {u.data_allowance_mb:,} MB data, {u.voice_min:,} min calls, "
                        f"{u.sms:,} SMS, {u.intl_voice_min:,} min international, roaming data outside the EU: {roam}, "
                        f"EU roaming {u.eu_roaming_data_mb:,} MB.", f"adb:bills:{row.id}"))
        ex = explain(b, prev)
        docs.append(Doc(f"{sub_id}:change:{b.cycle}", sub_id, "bill_change", b.cycle,
                        f"Why the {_month(b.cycle)} bill changed",
                        " ".join([*ex.facts, *ex.usage_changes]),
                        f"explainer:{row.id}" + (f" vs {prev.cycle}" if prev else "")))
        prev = b

    cycle = cycle_of(now)
    counters = state.counters(sub_id, cycle)
    # the same rating path as the home screen and the bill run (tariff changes, mid-month join)
    live = unbilled(state, sub_id, sub.plan_code, now, engine)
    lu = live.usage
    offset = state.committed_offset(partition_for(sub_id, state.partitions))
    day = now.day
    proj = int(lu.data_mb / max(day, 1) * monthrange(now.year, now.month)[1])
    # no clock time in the text: the content hash must change only when the facts change (a per-minute timestamp
    # re-embedded every customer on every sweep)
    docs.append(Doc(f"{sub_id}:usage:{cycle}", sub_id, "usage_live", cycle, f"Usage so far in {_month(cycle)}",
                    f"As of {_day(now)} (day {day} of the month), the usage records received for "
                    f"{_month(cycle)} add up to {lu.data_mb:,} MB of {lu.data_allowance_mb:,} MB data "
                    f"({data_usage_pct(plan, counters):.0f}%), {lu.voice_min:,} min calls, {lu.sms:,} SMS, "
                    f"{lu.intl_voice_min:,} min international, EU roaming {lu.eu_roaming_data_mb:,} MB, roaming outside "
                    f"the EU {sum(lu.roaming_data_mb.values()):,} MB. At this pace data use would reach about "
                    f"{proj:,} MB by the end of the month. Unbilled so far: {_money(live.total, cur)} including VAT.",
                    f"valkey:u:{{p{partition_for(sub_id, state.partitions)}}}:{sub_id}:{cycle}@offset{offset}"))
    if notes:
        docs.append(Doc(f"{sub_id}:alerts", sub_id, "alerts", None, "Data alerts sent",
                        " ".join(f"{_day(n.created_at)}: {n.title}. {n.body}" for n in notes),
                        "adb:notifications"))
    if pays:
        docs.append(Doc(f"{sub_id}:payments", sub_id, "payments", None, "Payments",
                        " ".join(f"{_day(p.created_at)}: payment of {_money(p.amount, p.currency)} for "
                                 + (f"the {_month(p.bill_id.split(':')[1])} bill" if p.bill_id else "usage paid in advance")
                                 + f": {PAY_WORDS[p.status]}." for p in pays if p.status in PAY_WORDS),
                        "adb:payments"))
    return docs


PAY_WORDS = {"PAID": "paid", "FAILED": "declined, nothing was charged", "PENDING": "waiting for the bank to confirm"}


def _day(d) -> str:
    return f"{d.day} {d:%B %Y}"


def upsert(engine: Engine, embedder: Embedder, docs: list[Doc], _retry: bool = True) -> dict:
    """Insert or update by content hash. Two writers (assistant + notifier) inserting the same new doc at once
    collide on the primary key: the loser retries as an update instead of failing the customer's request."""
    from sqlalchemy.exc import IntegrityError
    try:
        return _upsert(engine, embedder, docs)
    except IntegrityError:
        if not _retry:
            raise
        return upsert(engine, embedder, docs, _retry=False)


def _upsert(engine: Engine, embedder: Embedder, docs: list[Doc]) -> dict:
    with engine.connect() as cx:
        existing = dict(cx.execute(select(context_docs.c.id, context_docs.c.content_hash)
                                   .where(context_docs.c.id.in_([d.id for d in docs]))).all())
    changed = [d for d in docs if existing.get(d.id) != d.content_hash]
    if not changed:
        return {"docs": len(docs), "embedded": 0}
    vectors = embedder.embed([f"{d.title}. {d.body}" for d in changed])
    oracle = engine.dialect.name == "oracle"
    with engine.begin() as cx:
        for d, v in zip(changed, vectors, strict=True):
            values = {"subscriber_id": d.subscriber_id, "doc_type": d.doc_type, "cycle": d.cycle, "title": d.title, "body": d.body,
                          "source_ref": d.source_ref, "content_hash": d.content_hash, "embedding_json": json.dumps(v),
                          "updated_at": datetime.now(UTC)}
            if d.id in existing:
                cx.execute(update(context_docs).where(context_docs.c.id == d.id).values(**values))
            else:
                cx.execute(insert(context_docs).values(id=d.id, **values))
            if oracle:
                cx.execute(text("UPDATE context_docs SET embedding = :v WHERE id = :id"),
                           {"v": array.array("f", v), "id": d.id})
    return {"docs": len(docs), "embedded": len(changed)}


def refresh_subscriber(engine: Engine, state: UsageState, embedder: Embedder, sub_id: str) -> dict:
    docs = subscriber_docs(engine, state, sub_id)
    res = upsert(engine, embedder, docs)
    # docs no longer generated (last month's live usage, deleted bills/payments) must not stay searchable
    with engine.begin() as cx:
        gone = cx.execute(delete(context_docs).where(context_docs.c.subscriber_id == sub_id,
                                                     context_docs.c.id.not_in([d.id for d in docs]))).rowcount
    return res | {"removed": gone}


def refresh_global(engine: Engine, embedder: Embedder) -> dict:
    return upsert(engine, embedder, global_docs())


def purge_subscriber(engine: Engine, sub_id: str) -> None:
    with engine.begin() as cx:
        cx.execute(delete(context_docs).where(context_docs.c.subscriber_id == sub_id))

