"""Agent tools. Every tool is READ-only and scoped to the caller (no subscriber argument exists, so a
tool can never be pointed at another customer). Each result carries a `source` so answers can cite it.
Only the Broker may execute tools."""
from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from telco_common.billing import rate, split_counters
from telco_common.catalog import COUNTRIES, MB, get_addon, get_plan
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


def _due(due_date, total, paid) -> dict:
    """Due date plus the day counts the assistant may quote (computed here: the model must not do date maths)."""
    d = (due_date if due_date.tzinfo else due_date.replace(tzinfo=UTC)).date()
    out: dict = {"due_date": f"{d.day} {d:%B %Y}"}   # in words: the model quotes what it is given
    if total > paid:
        days = (datetime.now(UTC).date() - d).days
        out.update({"payment_status": "overdue" if days > 0 else "not yet due"})
        out.update({"days_overdue": days} if days > 0 else {"days_until_due": -days})
    else:
        out["payment_status"] = "paid"
    return out


def _month(cycle: str) -> str:
    return datetime(int(cycle[:4]), int(cycle[5:]), 1).strftime("%B %Y")


def t_list_bills(args: dict, ctx: ToolContext) -> dict:
    rows = list_bills(ctx.engine, ctx.subscriber_id, int(args.get("months", 6)))   # recent half-year unless asked
    return {"source": "adb:bills", "bills": [
        {"cycle": r.cycle, "month": _month(r.cycle), "total": fmt_money(r.total, r.currency),
         "paid_so_far": fmt_money(r.amount_paid, r.currency), "outstanding": fmt_money(r.total - r.amount_paid, r.currency),
         **_due(r.due_date, r.total, r.amount_paid)} for r in rows]}


def t_get_bill(args: dict, ctx: ToolContext) -> dict:
    row = get_bill_row(ctx.engine, ctx.subscriber_id, args["cycle"])
    if row is None:
        return {"error": f"no bill for {args['cycle']}"}
    b = row_to_bill(row)
    cur = b.currency
    return {"source": f"adb:bills:{row.id}", "month": _month(b.cycle),
            "total": fmt_money(b.total, cur), "net": fmt_money(b.subtotal, cur), "vat": fmt_money(b.vat, cur),
            "vat_rate": f"{int(float(b.vat_rate) * 100)}%", "paid_so_far": fmt_money(row.amount_paid, cur),
            "outstanding": fmt_money(row.total - row.amount_paid, cur),
            **_due(row.due_date, row.total, row.amount_paid),
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


OVERAGE_WORDS = {"THROTTLE": "beyond the allowance the speed is reduced; there is no extra charge",
                 "CHARGE": "every MB beyond the allowance is charged"}


def _data_position(plan_mb: int, counters: dict[str, int], extra_mb: int = 0) -> dict:
    """Plan allowance (pro-rated after a tariff change), add-ons, total, used, remaining / over, in MB."""
    _, _, addons = split_counters(counters)
    used_mb = -(-(counters.get("DATA", 0) + counters.get("ROAMING_DATA:EU", 0)) // MB)
    addon_mb = sum(get_addon(c).data_mb * n for c, n in addons.items())
    total = plan_mb + addon_mb + extra_mb
    pos = {"plan_data": f"{plan_mb:,} MB",
           "add_ons_this_month": [f"{get_addon(c).name} x{n} (+{get_addon(c).data_mb * n:,} MB)" for c, n in addons.items()] or "none",
           "total_allowance": f"{total:,} MB", "data_used": f"{used_mb:,} MB"}
    if used_mb <= total:
        pos["data_remaining"] = f"{total - used_mb:,} MB"
    else:
        pos["data_remaining"] = "0 MB"
        pos["over_allowance_by"] = f"{used_mb - total:,} MB"
    return pos


def _live(ctx: ToolContext):
    """The current month exactly as the app shows it: pro-rated by tariff segments (services.bills.unbilled),
    not the whole month on today's plan. -> (sub, plan, counters, bill, plan_mb)."""
    from ..services.bills import unbilled

    sub = _sub(ctx)
    plan = get_plan(sub.plan_code)
    counters = ctx.state.counters(ctx.subscriber_id, cycle_of(datetime.now(UTC)))
    b = unbilled(ctx.state, ctx.subscriber_id, sub.plan_code, engine=ctx.engine)
    _, _, addons = split_counters(counters)
    plan_mb = b.usage.data_allowance_mb - sum(get_addon(c).data_mb * n for c, n in addons.items())
    return sub, plan, counters, b, max(plan_mb, 0)


def _overage(plan) -> str:
    return OVERAGE_WORDS.get(str(plan.overage_policy).split(".")[-1].upper(), str(plan.overage_policy))


def t_current_usage(args: dict, ctx: ToolContext) -> dict:
    _, plan, counters, b, plan_mb = _live(ctx)
    now = datetime.now(UTC)
    cycle = cycle_of(now)
    u = b.usage
    days = monthrange(now.year, now.month)[1]   # 28-31: the cycle is the calendar month
    proj = int(u.data_mb / max(now.day, 1) * days)
    pos = _data_position(plan_mb, counters)
    split = _allowance_by_tariff(ctx, cycle)
    if split:   # a tariff change this month: say where the pro-rated plan data comes from (never guessed)
        pos = {"plan_data": pos["plan_data"], "plan_data_by_tariff": split, **{k: v for k, v in pos.items() if k != "plan_data"}}
    total = u.data_allowance_mb
    from ..services.payments import advance_paid_this_month

    adv = advance_paid_this_month(ctx.engine, ctx.subscriber_id)
    money = {"unbilled_lines_before_vat": [f"{x.label}: {fmt_money(x.amount, b.currency)}" for x in b.lines],
             "unbilled_subtotal_before_vat": fmt_money(b.subtotal, b.currency), "vat": fmt_money(b.vat, b.currency),
             "unbilled_total_incl_vat": fmt_money(b.total, b.currency)}
    if adv:
        money |= {"advance_paid_this_month": fmt_money(adv, b.currency),
                  "still_to_pay_this_month": fmt_money(max(b.total - adv, 0), b.currency),
                  "advance_note": "credited on this month's bill; anything used after it is added on top"}
    return {"source": f"valkey:u:{{p{partition_for(ctx.subscriber_id, ctx.state.partitions)}}}:{cycle}",
            "as_of": now.strftime("%d %B %Y %H:%M UTC"), "day_of_cycle": now.day, "current_plan": plan.name,
            **pos, "data_used_pct": f"{(u.data_mb * 100 / total) if total else 0:.0f}%",
            "projected_month_end": f"{proj:,} MB", "calls": f"{u.voice_min:,} min", "sms": u.sms,
            **money, "beyond_the_allowance": _overage(plan)}


def _allowance_by_tariff(ctx: ToolContext, cycle: str) -> list[str] | None:
    """["Mobile M, 22 of 30 days: 22,000 MB", "Mobile S, 8 of 30 days: 2,667 MB"] when the tariff changed this
    month (the same pro-rating as the bill: plan data x days / days in month, rounded half up)."""
    from decimal import ROUND_HALF_UP, Decimal

    from ..services.bills import cycle_bounds
    from ..services.plan_history import segments

    seg = segments(ctx.engine, ctx.subscriber_id, cycle, _sub(ctx).plan_code)
    if not seg or len(seg) < 2:
        return None
    start, end = cycle_bounds(cycle)
    month = (end.date() - start.date()).days
    share = lambda mb, d: int((Decimal(mb) * d / month).quantize(Decimal(1), ROUND_HALF_UP))
    return [f"{p.name}, {d} of {month} days: {share(p.data_mb, d):,} MB" for p, d in seg]


def cycle_bounds_now():
    from ..services.bills import cycle_bounds
    return cycle_bounds(cycle_of(datetime.now(UTC)))


def _month_if_switched_today(ctx: ToolContext, sub, new_code: str) -> int | None:
    """This month's whole unbilled total if the tariff changed today: the same rating path as the bill
    (rate_cycle), with the real tariff segments up to yesterday and the new plan from today."""
    from ..services.bills import cycle_bounds, first_billable_day, rate_cycle
    from ..services.plan_history import segments

    now = datetime.now(UTC)
    cycle = cycle_of(now)
    start, end = cycle_bounds(cycle)
    total, before = (end.date() - start.date()).days, (now.date() - start.date()).days
    seg = segments(ctx.engine, sub.id, cycle, sub.plan_code) or [(get_plan(sub.plan_code), total)]
    out, day = [], 0
    for p, n in seg:            # keep the days before today exactly as they were billed
        keep = max(0, min(n, before - day))
        if keep:
            out.append((p, keep))
        day += n
    out.append((get_plan(new_code), total - sum(n for _, n in out)))
    try:
        return rate_cycle(ctx.state, sub.id, cycle, new_code, ctx.state.counters(sub.id, cycle), out,
                          first_day=first_billable_day(ctx.engine, sub.id, cycle)).total
    except Exception:  # noqa: BLE001 - a preview is optional; never fail the catalog for it
        return None


def _addon_effect(ctx: ToolContext, add_mb: int) -> str | None:
    """What an add-on would do THIS month, computed (the model got "6,084 MB used, 3,000 + 5,000 MB" wrong when no
    add-on could be proposed, e.g. orders blocked by an overdue bill)."""
    try:
        _, plan, counters, _, plan_mb = _live(ctx)
    except Exception:  # noqa: BLE001 - a preview is optional; never fail the catalog for it
        return None
    after = _data_position(plan_mb, counters, extra_mb=add_mb)
    if "over_allowance_by" in after:
        return (f"allowance would be {after['total_allowance']} with {after['data_used']} used: still over by "
                f"{after['over_allowance_by']} ({_overage(plan)})")
    return f"allowance would be {after['total_allowance']} with {after['data_used']} used: within it, {after['data_remaining']} left"


def t_catalog(args: dict, ctx: ToolContext) -> dict:
    """Tariffs and add-ons the customer can actually order (their country only), with the current plan marked."""
    from telco_common.catalog import ADDONS, plans_for

    sub = _sub(ctx)
    cur = get_plan(sub.plan_code)
    cur_cfg = COUNTRIES[cur.country]
    plans = sorted(plans_for(cur.country), key=lambda p: p.data_mb)
    def money(minor: int) -> str:
        return fmt_money(minor, cur_cfg.currency)

    # this month if the tariff changed today, next to staying: prod (PL) quoted "422.38 PLN if you switch today" as if
    # it were a saving; staying was 344.30 PLN so far, because a switch pro-rates the current allowance to the days
    # before it, so usage already made counts against a smaller allowance
    stay = _month_if_switched_today(ctx, sub, cur.code)
    days_before = (datetime.now(UTC).date() - cycle_bounds_now()[0].date()).days

    def _if_switched(p) -> dict:
        v = _month_if_switched_today(ctx, sub, p.code)
        if v is None:
            return {}
        out = {"this_month_total_if_switched_today": money(v)}
        if stay is not None and v != stay:
            out["compared_with_staying"] = (
                f"{money(abs(v - stay))} {'more' if v > stay else 'less'} than staying on {cur.name} ({money(stay)} so far "
                f"this month)" + (f": {cur.name}'s allowance would count for only the {days_before} days before the switch"
                                  if v > stay and days_before else ""))
        return out

    def net(minor: int) -> str:   # catalog prices are before VAT; bills and unbilled totals include it
        gross = int((Decimal(minor) * (1 + cur_cfg.vat_rate)).quantize(Decimal(1), ROUND_HALF_UP))
        return f"{money(minor)} + VAT ({money(gross)} incl. VAT)"
    return {"source": f"catalog:{cur.country}",
            "plans": [{"code": p.code, "name": p.name, "monthly_fee": net(p.monthly_fee), "data": f"{p.data_mb:,} MB",
                       "calls": "unlimited" if p.voice_min is None else f"{p.voice_min:,} min",
                       "beyond_the_allowance": _overage(p),
                       **({"current": True} if p.code == cur.code else _if_switched(p))}
                      for p in plans],
            "largest_plan": plans[-1].name if plans else None,
            "add_ons": [{"code": a.code, "name": a.name, "data": f"{a.data_mb:,} MB", "price": net(a.price),
                         "applies": "this month only, on top of the plan",
                         **({"effect_this_month": eff} if (eff := _addon_effect(ctx, a.data_mb)) else {})}
                        for a in ADDONS.values() if a.country == cur.country],
            "rules": {"tariff_change": "takes effect today once approved; this month is pro-rated by day: the old plan "
                                      "until yesterday, the new plan from today (fee and allowances)",
                      "add_on": "active a few seconds after approval; adds data to this month only; charged on the next bill",
                      "orders_blocked_when": "a bill is overdue: it must be paid first"},
            "note": "these are all the plans and add-ons available; there are no others"}


def t_search_knowledge(args: dict, ctx: ToolContext) -> dict:
    """L2: hybrid BM25 + vector (Oracle 23ai) + RRF over the context spine, graded (CRAG)."""
    from .retrieve import hybrid

    docs, _ = hybrid(ctx.engine, ctx.embedder, args["query"], ctx.subscriber_id, top_k=6)
    chunks = [{"title": d.title, "text": d.body[:700]} for d in docs]   # only what answers the question
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
    return {"source": "spine:search", "chunks": chunks}


def t_cdr_evidence(args: dict, ctx: ToolContext) -> dict:
    from ..services.evidence import cdr_evidence

    ev = cdr_evidence(ctx.state, ctx.subscriber_id, args["cycle"])
    return {"source": ev["source"], "usage_records": ev["cdr_count"], "first": ev["first_event"], "last": ev["last_event"],
            "everything_matches_the_bill": ev["all_match"],
            "totals": [f"{x['key']}: {x['cdr_records']} usage records = {x['from_cdrs']:,} {x['unit']}"
                       f" ({ {'match': 'matches the meter', 'arriving': 'newest records still being archived'}.get(x.get('state', 'match' if x['match'] else 'differs'), 'does not match the meter')})"
                       for x in ev["reconciliation"]]}


def t_payments(args: dict, ctx: ToolContext) -> dict:
    with ctx.engine.connect() as cx:
        rows = cx.execute(select(payments).where(payments.c.subscriber_id == ctx.subscriber_id)
                          .order_by(payments.c.created_at.desc()).limit(10)).all()
    # Only what a customer would recognise as a payment: completed, declined or awaiting the bank. A checkout
    # that was opened and closed without paying (CREATED / EXPIRED) is not a payment and is left out.
    words = {"PAID": "paid", "FAILED": "declined, nothing was charged", "PENDING": "waiting for the bank to confirm"}
    shown = [r for r in rows if r.status in words]
    return {"source": "adb:payments", "payments": [
        {"date": f"{r.created_at.day} {r.created_at:%B %Y}", "for": (f"{_month(r.bill_id.split(':')[1])} bill" if r.bill_id
                                                                        else "usage paid in advance"),
         "amount": fmt_money(r.amount, r.currency), "result": words[r.status]} for r in shown],
        **({} if shown else {"note": "No payments made yet."})}


ORDER_WORDS = {"PROPOSED": "prepared, not ordered: waiting for the customer's approval", "APPROVED": "approved, being processed",
               "SUBMITTED": "approved, waiting for the network to confirm", "COMPLETED": "active",
               "FAILED": "could not be completed (not charged)", "DECLINED": "declined by the customer",
               "EXPIRED": "offer expired"}


def t_orders(args: dict, ctx: ToolContext) -> dict:
    """The customer's recent add-on and tariff orders and their real status (e.g. after 'I just approved it')."""
    from ..db import orders
    from ..workflows.orders import expired
    with ctx.engine.connect() as cx:
        rows = cx.execute(select(orders).where(orders.c.subscriber_id == ctx.subscriber_id)
                          .order_by(orders.c.created_at.desc()).limit(8)).all()
    out = []
    for r in rows:
        item = (get_addon(r.addon_code).name if r.kind == "ADDON" else f"switch to {get_plan(r.addon_code).name}")
        status = "EXPIRED" if expired(r) else r.status   # an offer past its validity is not "waiting" any more
        out.append({"date": f"{r.created_at.day} {r.created_at:%B %Y} {r.created_at:%H:%M} UTC", "order": item,
                    "status": ORDER_WORDS.get(status, status.lower())})
    return {"source": "adb:orders", "orders": out or "no orders yet"}


def open_offer_card(ctx: ToolContext) -> dict | None:
    """The approval card of the customer's newest open add-on / tariff offer (still waiting, not expired), so an
    answer that points to it ("it's waiting for your approval below") shows it again. Prod: "did it go through?"
    was answered that way, the check found no card in this turn, and the retry prepared a NEW add-on, which
    replaced the one the customer was looking at."""
    from ..db import orders
    from ..workflows.orders import expired
    with ctx.engine.connect() as cx:
        r = cx.execute(select(orders).where(orders.c.subscriber_id == ctx.subscriber_id, orders.c.status == "PROPOSED",
                                            orders.c.kind.in_(("ADDON", "PLAN")))
                       .order_by(orders.c.created_at.desc()).limit(1)).first()
    if r is None or expired(r):
        return None
    if r.kind == "ADDON":
        a = get_addon(r.addon_code)
        return {"type": "proposal", "kind": "ADDON", "order_id": r.id, "add_on": a.name, "extra_data": f"{a.data_mb:,} MB",
                "price": f"{fmt_money(r.price, r.currency)} + VAT"}
    return {"type": "proposal", "kind": "PLAN", "order_id": r.id, "add_on": get_plan(r.addon_code).name,
            "extra_data": r.detail or "", "price": f"{fmt_money(r.price, r.currency)} / month"}


def t_alerts(args: dict, ctx: ToolContext) -> dict:
    with ctx.engine.connect() as cx:
        rows = cx.execute(select(notifications).where(notifications.c.subscriber_id == ctx.subscriber_id)
                          .order_by(notifications.c.created_at.desc()).limit(10)).all()
    return {"source": "adb:notifications", "alerts": [f"{r.created_at.day} {r.created_at:%B %Y}: {r.title}" for r in rows]}


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
    # the honest effect of this add-on on THIS month: does it actually give the customer data back?
    _, plan, counters, _, plan_mb = _live(ctx)
    after = _data_position(plan_mb, counters, extra_mb=pv["data_mb"])
    helps = "over_allowance_by" not in after
    return {"source": f"orders:{o['order_id']}", "proposal": True, "order_id": o["order_id"],
            "add_on": pv["name"], "extra_data": f"{pv['data_mb']:,} MB", "price": f"{fmt_money(pv['price'], pv['currency'])} + VAT",
            "after_this_add_on": after,
            "effect": ("after it, the customer has data left this month" if helps else
                       f"even after it the customer is still {after['over_allowance_by']} over the allowance: "
                       + _overage(plan)),
            "status": "waiting for the customer's approval in the card below (charged on the next bill)"}


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
                "overdue": due + timedelta(days=1) <= datetime.now(UTC),   # after the end of the due day **_due(b.due_date, b.total, b.amount_paid),
                "status": "waiting for the customer to pay in the secure checkout"}
    from ..services.payments import advance_paid_this_month

    sub = _sub(ctx)
    cur = unbilled(ctx.state, ctx.subscriber_id, sub.plan_code, engine=ctx.engine)
    if cur.total <= 0:
        return {"source": "usage:current", "nothing_to_pay": True, "message": "Nothing has been used this month yet."}
    paid = advance_paid_this_month(ctx.engine, ctx.subscriber_id)   # the checkout charges only what's left
    if cur.total - paid <= 0:
        return {"source": f"usage:{cur.cycle}", "nothing_to_pay": True,
                "message": f"This month so far ({fmt_money(cur.total, cur.currency)}) is already covered by advance "
                           f"payments of {fmt_money(paid, cur.currency)}."}
    return {"source": f"usage:{cur.cycle}", "proposal": True, "payment": True, "purpose": "ADVANCE", "bill_id": None,
            "cycle": cur.cycle, "amount": fmt_money(cur.total - paid, cur.currency),
            **({"already_paid_in_advance": fmt_money(paid, cur.currency)} if paid else {}),
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
            "monthly_fee": f"{fmt_money(pv['old_monthly_fee'], pv['currency'])} -> {fmt_money(pv['monthly_fee'], pv['currency'])} (before VAT)",
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
    Tool("get_plans_and_addons", "All tariffs and data add-ons the customer can order in their country (fees, data, "
         "which plan is current, which is the largest). Check it before suggesting a plan or add-on.", NONE, t_catalog),
    Tool("get_orders", "The customer's recent add-on and tariff-change orders and their status (proposed, being "
         "processed, active, declined, failed). Use it when they mention an order or say they approved something.",
         NONE, t_orders),
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
