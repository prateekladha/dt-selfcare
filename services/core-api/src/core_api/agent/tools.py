"""Agent tools. Every tool is READ-only and scoped to the caller (no subscriber argument exists, so a
tool can never be pointed at another customer). Each result carries a `source` so answers can cite it.
Only the Broker may execute tools."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from telco_common.billing import data_usage_pct, rate
from telco_common.catalog import COUNTRIES, get_plan
from telco_common.explainer import explain, fmt_money
from telco_common.models import cycle_of
from telco_common.partitioning import partition_for

from ..db import notifications, payments, subscribers
from ..services.bills import get_bill_row, list_bills, previous_cycle, row_to_bill


@dataclass
class ToolContext:
    subscriber_id: str
    country: str
    engine: object
    state: object
    embedder: object
    llm: object | None = None
    trace: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[dict, ToolContext], dict]
    tier: str = "read"


def _sub(ctx: ToolContext):
    with ctx.engine.connect() as cx:
        return cx.execute(select(subscribers).where(subscribers.c.id == ctx.subscriber_id)).one()


def _month(cycle: str) -> str:
    return datetime(int(cycle[:4]), int(cycle[5:]), 1).strftime("%B %Y")


def t_list_bills(args: dict, ctx: ToolContext) -> dict:
    rows = list_bills(ctx.engine, ctx.subscriber_id, int(args.get("months", 12)))
    return {"source": "adb:bills", "bills": [
        {"cycle": r.cycle, "month": _month(r.cycle), "total": fmt_money(r.total, r.currency), "status": r.status,
         "paid": fmt_money(r.amount_paid, r.currency), "due": r.due_date.date().isoformat()} for r in rows]}


def t_get_bill(args: dict, ctx: ToolContext) -> dict:
    row = get_bill_row(ctx.engine, ctx.subscriber_id, args["cycle"])
    if row is None:
        return {"error": f"no bill for {args['cycle']}"}
    b = row_to_bill(row)
    cur = b.currency
    return {"source": f"adb:bills:{row.id}", "month": _month(b.cycle), "status": row.status,
            "total": fmt_money(b.total, cur), "net": fmt_money(b.subtotal, cur), "vat": fmt_money(b.vat, cur),
            "vat_rate": f"{int(float(b.vat_rate) * 100)}%", "due": row.due_date.date().isoformat(),
            "lines": [f"{x.label}: {x.quantity:,} {x.unit} = {fmt_money(x.amount, cur)}" for x in b.lines],
            "usage": {"data": f"{b.usage.data_mb:,} MB of {b.usage.data_allowance_mb:,} MB",
                      "calls": f"{b.usage.voice_min:,} min", "sms": b.usage.sms,
                      "roaming_outside_eu": {z: f"{mb:,} MB" for z, mb in b.usage.roaming_data_mb.items()}}}


def t_compare_bills(args: dict, ctx: ToolContext) -> dict:
    cycle = args["cycle"]
    row = get_bill_row(ctx.engine, ctx.subscriber_id, cycle)
    if row is None:
        return {"error": f"no bill for {cycle}"}
    prev = get_bill_row(ctx.engine, ctx.subscriber_id, previous_cycle(cycle))
    ex = explain(row_to_bill(row), row_to_bill(prev) if prev else None)
    return {"source": f"rating-engine:compare:{cycle}", "facts": [*ex.facts, *ex.usage_changes],
            "changes": [{"item": d.label, "change": fmt_money(d.delta, ex.currency), "detail": d.detail} for d in ex.drivers]}


def t_current_usage(args: dict, ctx: ToolContext) -> dict:
    sub = _sub(ctx)
    plan = get_plan(sub.plan_code)
    now = datetime.now(UTC)
    cycle = cycle_of(now)
    counters = ctx.state.counters(ctx.subscriber_id, cycle)
    b = rate(ctx.subscriber_id, cycle, plan, counters)
    u = b.usage
    pct = data_usage_pct(plan, counters)
    days = 30
    proj = int(u.data_mb / max(now.day, 1) * days)
    return {"source": f"valkey:u:{{p{partition_for(ctx.subscriber_id, ctx.state.partitions)}}}:{cycle}",
            "as_of": now.strftime("%d %B %Y %H:%M UTC"), "day_of_cycle": now.day,
            "data_used": f"{u.data_mb:,} MB", "data_allowance": f"{u.data_allowance_mb:,} MB", "data_used_pct": f"{pct:.0f}%",
            "projected_month_end": f"{proj:,} MB", "calls": f"{u.voice_min:,} min", "sms": u.sms,
            "unbilled_total": fmt_money(b.total, b.currency),
            "unbilled_lines": [f"{x.label}: {fmt_money(x.amount, b.currency)}" for x in b.lines],
            "overage_policy": plan.overage_policy}


def t_search_knowledge(args: dict, ctx: ToolContext) -> dict:
    """L2: hybrid BM25 + vector (Oracle 23ai) + RRF over the context spine, graded (CRAG)."""
    from .retrieve import hybrid

    docs, meta = hybrid(ctx.engine, ctx.embedder, args["query"], ctx.subscriber_id, top_k=6)
    chunks = [{"id": d.id, "title": d.title, "text": d.body[:900], "source": d.source_ref, "scores": d.scores} for d in docs]
    grade = "ambiguous"
    if ctx.llm is not None and chunks:
        import json

        listing = "\n".join(f"{i}: {c['title']}: {c['text'][:240]}" for i, c in enumerate(chunks))
        try:
            res = ctx.llm.chat("grade", [
                {"role": "system", "content": 'Grade retrieved snippets for the query. JSON: {"grade": "correct|ambiguous|incorrect", "relevant": [indices]}'},
                {"role": "user", "content": f"Query: {args['query']}\n{listing}"}], json_mode=True)
            ctx.trace.setdefault("stages", []).append(res.as_trace())
            g = json.loads(res.text)
            grade = g.get("grade", "ambiguous")
            keep = [i for i in g.get("relevant", []) if isinstance(i, int) and 0 <= i < len(chunks)]
            if keep:
                chunks = [chunks[i] for i in keep]
        except Exception as e:  # noqa: BLE001 - grader is advisory (fails open to ambiguous)
            ctx.trace.setdefault("degraded", []).append(f"grader: {type(e).__name__}")
    if grade == "incorrect":
        return {"source": "spine:search", "grade": grade, "chunks": [],
                "note": "Nothing relevant found. Do not guess; say what is missing."}
    return {"source": "spine:search", "grade": grade, "vector_engine": meta["vector_engine"], "chunks": chunks}


def t_cdr_evidence(args: dict, ctx: ToolContext) -> dict:
    from ..services.evidence import cdr_evidence

    ev = cdr_evidence(ctx.state, ctx.subscriber_id, args["cycle"])
    return {"source": ev["source"], "cdr_count": ev["cdr_count"], "first": ev["first_event"], "last": ev["last_event"],
            "all_match": ev["all_match"], "totals": [f"{x['key']}: {x['cdr_records']} CDRs = {x['from_cdrs']:,} {x['unit']}"
                                                      f" ({'matches' if x['match'] else 'differs from'} running total)"
                                                      for x in ev["reconciliation"]]}


def t_payments(args: dict, ctx: ToolContext) -> dict:
    with ctx.engine.connect() as cx:
        rows = cx.execute(select(payments).where(payments.c.subscriber_id == ctx.subscriber_id)
                          .order_by(payments.c.created_at.desc()).limit(10)).all()
    return {"source": "adb:payments", "payments": [
        {"date": r.created_at.date().isoformat(), "purpose": r.purpose.lower(), "amount": fmt_money(r.amount, r.currency),
         "status": r.status, "bill": r.bill_id.split(":")[1] if r.bill_id else None} for r in rows]}


def t_alerts(args: dict, ctx: ToolContext) -> dict:
    with ctx.engine.connect() as cx:
        rows = cx.execute(select(notifications).where(notifications.c.subscriber_id == ctx.subscriber_id)
                          .order_by(notifications.c.created_at.desc()).limit(10)).all()
    return {"source": "adb:notifications", "alerts": [f"{r.created_at.date().isoformat()}: {r.title}" for r in rows]}


def t_simulate(args: dict, ctx: ToolContext) -> dict:
    from ..services.simulate import SCENARIOS, scenario_counters

    sub = _sub(ctx)
    plan = get_plan(sub.plan_code)
    key = args["scenario"]
    if key not in SCENARIOS:
        return {"error": f"unknown scenario; choose one of {sorted(SCENARIOS)}"}
    last = list_bills(ctx.engine, ctx.subscriber_id, 1)
    counters = scenario_counters(key)
    if last:
        for k, v in ctx.state.counters(ctx.subscriber_id, last[-1].cycle).items():
            counters[k] = counters.get(k, 0) + v
    b = rate(ctx.subscriber_id, cycle_of(datetime.now(UTC)), plan, counters)
    cur = COUNTRIES[plan.country].currency
    return {"source": f"rating-engine:simulate:{key}", "scenario": SCENARIOS[key]["title"],
            "based_on": f"your {_month(last[-1].cycle)} usage" if last else "the plan only",
            "estimated_total": fmt_money(b.total, cur), "lines": [f"{x.label}: {fmt_money(x.amount, cur)}" for x in b.lines]}


def t_propose_addon(args: dict, ctx: ToolContext) -> dict:
    """WRITE (propose tier): prepares an add-on order for the customer to approve. Nothing changes until
    the customer approves in the app; approval starts a durable workflow."""
    from ..workflows.orders import OrderError, propose_addon

    try:
        o = propose_addon(ctx.engine, ctx.subscriber_id, args["addon_code"], source="assistant")
    except OrderError as e:
        return {"error": e.message}
    pv = o["preview"]
    return {"source": f"orders:{o['order_id']}", "proposal": True, "order_id": o["order_id"],
            "add_on": pv["name"], "extra_data": f"{pv['data_mb']:,} MB", "price": fmt_money(pv["price"], pv["currency"]),
            "status": "waiting for the customer's approval"}


def t_propose_payment(args: dict, ctx: ToolContext) -> dict:
    """PAYMENT (propose tier): prepares a payment card; the customer pays in the secure checkout.
    purpose=bill -> the oldest unpaid bill; purpose=advance -> this month's unbilled amount so far."""
    from ..db import bills
    from ..services.bills import unbilled

    if args["purpose"] == "bill":
        with ctx.engine.connect() as cx:
            b = cx.execute(select(bills).where(bills.c.subscriber_id == ctx.subscriber_id, bills.c.total > bills.c.amount_paid)
                           .order_by(bills.c.cycle)).first()
        if b is None:
            return {"source": "adb:bills", "nothing_to_pay": True, "message": "All bills are paid."}
        due = b.due_date if b.due_date.tzinfo else b.due_date.replace(tzinfo=UTC)
        return {"source": f"adb:bills:{b.cycle}", "proposal": True, "payment": True, "purpose": "BILL", "bill_id": b.id,
                "cycle": b.cycle, "amount": fmt_money(b.total - b.amount_paid, b.currency),
                "due_date": due.date().isoformat(), "overdue": due < datetime.now(UTC),
                "status": "waiting for the customer to pay in the secure checkout"}
    sub = _sub(ctx)
    cur = unbilled(ctx.state, ctx.subscriber_id, sub.plan_code, engine=ctx.engine)
    if cur.total <= 0:
        return {"source": "usage:current", "nothing_to_pay": True, "message": "Nothing has been used this month yet."}
    return {"source": f"usage:{cur.cycle}", "proposal": True, "payment": True, "purpose": "ADVANCE", "bill_id": None,
            "cycle": cur.cycle, "amount": fmt_money(cur.total, cur.currency),
            "status": "waiting for the customer to pay in the secure checkout"}


def t_propose_plan_change(args: dict, ctx: ToolContext) -> dict:
    """WRITE (propose tier): prepares a tariff change for the customer to approve."""
    from ..workflows.orders import OrderError, propose_plan_change

    try:
        o = propose_plan_change(ctx.engine, ctx.subscriber_id, args["plan_code"], source="assistant")
    except OrderError as e:
        return {"error": e.message}
    pv = o["preview"]
    return {"source": f"orders:{o['order_id']}", "proposal": True, "order_id": o["order_id"],
            "new_tariff": pv["name"], "current_tariff": pv["from"],
            "monthly_fee": f"{fmt_money(pv['old_monthly_fee'], pv['currency'])} -> {fmt_money(pv['monthly_fee'], pv['currency'])}",
            "data": f"{pv['old_data']} -> {pv['data']}",
            "this_month_fee_prorated": f"{fmt_money(pv['this_month_fee'], pv['currency'])} ({pv['days_old']} days old tariff, {pv['days_new']} days new)",
            "status": "waiting for the customer's approval"}


CYCLE = {"type": "object", "properties": {"cycle": {"type": "string", "pattern": r"^\d{4}-\d{2}$",
                                                    "description": "billing month, YYYY-MM"}},
         "required": ["cycle"], "additionalProperties": False}
NONE = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("list_bills", "List the customer's recent bills (month, total, status).",
         {"type": "object", "properties": {"months": {"type": "integer", "minimum": 1, "maximum": 13}},
          "additionalProperties": False}, t_list_bills),
    Tool("get_bill", "Full detail of one issued bill: lines, VAT, usage, status.", CYCLE, t_get_bill),
    Tool("compare_bills", "Facts about how a bill changed versus the previous month (computed by the rating engine).",
         CYCLE, t_compare_bills),
    Tool("get_current_usage", "Live usage and unbilled amount for the current month from the CDR totals, with a month-end projection.",
         NONE, t_current_usage),
    Tool("search_knowledge", "Search the customer's records and the tariff/policy knowledge (hybrid lexical + semantic search, graded).",
         {"type": "object", "properties": {"query": {"type": "string", "minLength": 3, "maxLength": 200}},
          "required": ["query"], "additionalProperties": False}, t_search_knowledge),
    Tool("get_cdr_evidence", "Re-read the raw CDRs of a month from the archive and reconcile them with the running totals.",
         CYCLE, t_cdr_evidence),
    Tool("get_payments", "Recent payments and their status (status comes from the payment gateway's status API).",
         NONE, t_payments),
    Tool("get_alerts", "Data alerts sent to the customer.", NONE, t_alerts),
    Tool("simulate_usage", "Price a what-if scenario on top of the customer's last month of real usage.",
         {"type": "object", "properties": {"scenario": {"type": "string", "enum": [
             "us-trip-2-weeks", "eu-summer-holiday", "heavy-streaming", "calling-family-abroad", "light-month"]}},
          "required": ["scenario"], "additionalProperties": False}, t_simulate),
    Tool("propose_addon", "Prepare a data add-on purchase for the customer to approve (nothing is bought until they approve). "
         "Codes: DE-DATA-5GB, DE-DATA-20GB (Germany), PL-DATA-5GB (Poland).",
         {"type": "object", "properties": {"addon_code": {"type": "string", "enum": ["DE-DATA-5GB", "DE-DATA-20GB", "PL-DATA-5GB"]}},
          "required": ["addon_code"], "additionalProperties": False}, t_propose_addon, tier="propose"),
    Tool("propose_payment", "Prepare a payment for the customer to complete in the secure checkout: purpose 'bill' pays the "
         "oldest unpaid bill, 'advance' pays this month's usage so far. Use it when the customer wants to pay, when a bill is "
         "overdue, or when a purchase was refused because of an overdue bill.",
         {"type": "object", "properties": {"purpose": {"type": "string", "enum": ["bill", "advance"]}},
          "required": ["purpose"], "additionalProperties": False}, t_propose_payment, tier="propose"),
    Tool("propose_plan_change", "Prepare a tariff change for the customer to approve (nothing changes until they approve). "
         "Germany: DE-S (Mobile S, 10 GB), DE-M (Mobile M, 30 GB), DE-FLEX (Mobile Flex, 3 GB, pay per use); "
         "Poland: PL-S (Mobile S, 15 GB), PL-FLEX (Mobile Flex, 5 GB). Only tariffs of the customer's market.",
         {"type": "object", "properties": {"plan_code": {"type": "string", "enum": ["DE-S", "DE-M", "DE-FLEX", "PL-S", "PL-FLEX"]}},
          "required": ["plan_code"], "additionalProperties": False}, t_propose_plan_change, tier="propose"),
]}


def declarations(names: list[str]) -> list[dict]:
    return [{"type": "function", "function": {"name": TOOLS[n].name, "description": TOOLS[n].description,
                                              "parameters": TOOLS[n].parameters}} for n in names]
