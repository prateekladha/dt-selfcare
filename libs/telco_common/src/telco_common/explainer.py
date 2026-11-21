"""Deterministic bill explanation: "why did my bill change" + "top usage drivers".

This produces *facts* computed from two rated bills. The AI layer only rephrases these facts
in natural language and is never allowed to invent numbers; if the LLM is disabled or down,
these facts are rendered with templates, so the feature degrades instead of failing.
"""
from __future__ import annotations

from pydantic import BaseModel

from .billing import Bill, BillLine, LineCategory


class Driver(BaseModel):
    code: str
    label: str
    category: LineCategory
    previous: int
    current: int
    delta: int
    detail: str


class UsageDriver(BaseModel):
    code: str
    label: str
    amount: int
    share_pct: float
    detail: str


class Explanation(BaseModel):
    cycle: str
    previous_cycle: str | None
    currency: str
    previous_total: int | None
    current_total: int
    delta_total: int | None
    delta_pct: float | None
    direction: str  # UP | DOWN | SAME | FIRST_BILL
    drivers: list[Driver]
    top_usage_drivers: list[UsageDriver]
    usage_changes: list[str]
    facts: list[str]


def fmt_money(minor: int, currency: str) -> str:
    sign = "-" if minor < 0 else ""
    return f"{sign}{abs(minor) / 100:,.2f} {currency}"


def _qty(line: BillLine | None) -> str:
    return f"{line.quantity:,} {line.unit}" if line else "none"


def explain(current: Bill, previous: Bill | None, top_n: int = 3) -> Explanation:
    cur = current.currency
    cur_lines = {line.code: line for line in current.lines}
    prev_lines = {line.code: line for line in previous.lines} if previous else {}

    drivers: list[Driver] = []
    if previous:
        for code in cur_lines.keys() | prev_lines.keys():
            c, p = cur_lines.get(code), prev_lines.get(code)
            delta = (c.amount if c else 0) - (p.amount if p else 0)
            if delta == 0:
                continue
            ref = c or p
            assert ref is not None
            drivers.append(Driver(
                code=code, label=ref.label, category=ref.category,
                previous=p.amount if p else 0, current=c.amount if c else 0, delta=delta,
                detail=f"{ref.label}: {_qty(c)} this cycle vs {_qty(p)} last cycle "
                       f"({'+' if delta > 0 else ''}{fmt_money(delta, cur)} before VAT)",
            ))
        drivers.sort(key=lambda d: abs(d.delta), reverse=True)

    variable = [line for line in current.lines if line.category != LineCategory.PLAN and line.amount > 0]
    variable_total = sum(line.amount for line in variable) or 1
    top = [
        UsageDriver(code=line.code, label=line.label, amount=line.amount,
                    share_pct=round(line.amount * 100 / variable_total, 1),
                    detail=f"{line.label}: {line.quantity:,} {line.unit} = {fmt_money(line.amount, cur)}")
        for line in sorted(variable, key=lambda x: x.amount, reverse=True)[:top_n]
    ]

    usage_changes: list[str] = []
    if previous:
        pu, cu = previous.usage, current.usage
        for label, a, b, unit in [
            ("Mobile data", pu.data_mb, cu.data_mb, "MB"),
            ("Calls", pu.voice_min, cu.voice_min, "min"),
            ("International calls", pu.intl_voice_min, cu.intl_voice_min, "min"),
            ("Roaming data outside EU", sum(pu.roaming_data_mb.values()), sum(cu.roaming_data_mb.values()), "MB"),
        ]:
            if a == b:
                continue
            if a == 0:
                usage_changes.append(f"{label}: {b:,} {unit} (none last cycle)")
            else:
                usage_changes.append(f"{label}: {b:,} {unit} vs {a:,} {unit} ({(b - a) * 100 / a:+.0f}%)")
    cu = current.usage
    usage_changes.append(f"Data used: {cu.data_mb:,} MB of {cu.data_allowance_mb:,} MB allowance "
                         f"({cu.data_mb * 100 / max(cu.data_allowance_mb, 1):.0f}%)")

    if previous is None:
        direction, delta_total, delta_pct = "FIRST_BILL", None, None
    else:
        delta_total = current.total - previous.total
        delta_pct = round(delta_total * 100 / previous.total, 1) if previous.total else None
        direction = "UP" if delta_total > 0 else "DOWN" if delta_total < 0 else "SAME"

    facts: list[str] = []
    if previous is None:
        facts.append(f"This is your first bill: {fmt_money(current.total, cur)} including VAT.")
    elif direction == "SAME":
        facts.append(f"Your bill is unchanged at {fmt_money(current.total, cur)}.")
    else:
        word = "increased" if direction == "UP" else "decreased"
        facts.append(f"Your bill {word} by {fmt_money(abs(delta_total or 0), cur)}"
                     + (f" ({abs(delta_pct):.1f}%)" if delta_pct is not None else "")
                     + f", from {fmt_money(previous.total, cur)} to {fmt_money(current.total, cur)} including VAT.")
        facts.extend(f"{d.detail}." for d in drivers[:top_n])
    if top:
        facts.append("Biggest charges beyond your plan fee: " + "; ".join(t.detail for t in top) + ".")
    elif previous is not None:
        facts.append("You had no charges beyond your plan fee this cycle.")

    return Explanation(
        cycle=current.cycle, previous_cycle=previous.cycle if previous else None, currency=cur,
        previous_total=previous.total if previous else None, current_total=current.total,
        delta_total=delta_total, delta_pct=delta_pct, direction=direction,
        drivers=drivers, top_usage_drivers=top, usage_changes=usage_changes, facts=facts,
    )
