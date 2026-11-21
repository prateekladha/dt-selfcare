"""Bill explanation as ONE structured LLM call over computed facts (no agent loop needed).

Why not an agent here: the inputs are known up front (this bill, the previous bill, the computed change,
the relevant tariff rules), so a tool loop would only add latency and cost. The LLM writes the
explanation ("why it changed", "top usage drivers", tip) as JSON; every item must cite sources S#, and
every number in an item must appear in the sources it cites (verified, repaired up to twice).

Caching (L3): the system prompt + shared tariff knowledge is a stable prefix (provider prefix cache);
the full result is cached in Valkey by sha256(sources, language, model, prompt version) - an issued
bill is explained once; the current month re-explains only when its CDR totals change.
"""
from __future__ import annotations

import hashlib
import json

from telco_common.billing import Bill
from telco_common.catalog import COUNTRIES
from telco_common.explainer import explain, fmt_money

from .agent import numbers_in
from .llm import LLM, BudgetExceeded
from .spine import global_docs

PROMPT_VERSION = "explain-json-v2"
LANGS = {"en": "English", "de": "German", "pl": "Polish"}

SYSTEM = """You explain a mobile phone bill to the customer using ONLY the numbered sources.
Return JSON exactly in this shape:
{"summary": "<2-3 sentences: did the bill go up or down, by how much, and the main reason>",
 "drivers": [{"title": "<what changed, 2-5 words>", "impact": "<signed amount only, e.g. +7.40 EUR>", "why": "<one sentence>", "sources": ["S1"]}],
 "top_usage": [{"title": "<charge beyond the plan fee, 2-5 words>", "amount": "<amount only, e.g. 7.40 EUR>", "why": "<one sentence>", "sources": ["S2"]}],
 "tip": "<one practical tip or null>", "tip_sources": ["S#"]}
RULES: numbers and amounts must be copied exactly from the sources you list for that item; list the
largest changes first (max 4 drivers, max 3 top_usage); if nothing changed, drivers is []; if there are
no charges beyond the plan fee, top_usage is []. Write for a customer, plainly; no mention of sources or AI.

TARIFF KNOWLEDGE (always available as sources G1..Gn)
{knowledge}"""


def build_sources(cur: Bill, prev: Bill | None) -> dict[str, str]:
    c = COUNTRIES[cur.country].currency
    ex = explain(cur, prev)
    src = {
        "S1": "Computed comparison: " + " ".join([*ex.facts, *ex.usage_changes]),
        "S2": f"Bill {cur.cycle}: total {fmt_money(cur.total, c)} incl. VAT {fmt_money(cur.vat, c)}; lines: "
              + "; ".join(f"{x.label} {x.quantity:,} {x.unit} = {fmt_money(x.amount, c)}" for x in cur.lines),
    }
    if prev:
        src["S3"] = (f"Bill {prev.cycle}: total {fmt_money(prev.total, c)}; lines: "
                     + "; ".join(f"{x.label} {x.quantity:,} {x.unit} = {fmt_money(x.amount, c)}" for x in prev.lines))
    for i, d in enumerate(ex.drivers[:4], start=len(src) + 1):
        src[f"S{i}"] = f"Change in {d.label}: {d.detail}. Change amount {fmt_money(d.delta, c)} before VAT."
    return src


def _items_problems(out: dict, sources: dict[str, str]) -> list[str]:
    problems = []
    entries = [("summary", out.get("summary", ""), list(sources))]  # summary may use any source
    for k in ("drivers", "top_usage"):
        for i, it in enumerate(out.get(k) or []):
            text = " ".join(str(it.get(f, "")) for f in ("title", "impact", "amount", "why"))
            entries.append((f"{k}[{i}]", text, it.get("sources") or []))
    if out.get("tip"):
        entries.append(("tip", out["tip"], out.get("tip_sources") or list(sources)))
    for name, text, cites in entries:
        bad = [c for c in cites if c not in sources]
        if bad:
            problems.append(f"{name}: unknown sources {bad}")
        allowed = set().union(*(numbers_in(sources[c]) for c in cites if c in sources)) if cites else set()
        for tok in numbers_in(text):
            if tok not in allowed and len(tok) > 1:
                problems.append(f"{name}: number {tok} not in its sources")
                break
    return problems


def explain_bill(llm: LLM, r, cur: Bill, prev: Bill | None, lang: str = "en") -> dict:
    lang = lang if lang in LANGS else "en"
    sources = build_sources(cur, prev)
    knowledge_docs = sorted(global_docs(), key=lambda d: d.id)
    knowledge = {f"G{i}": f"{g.title}. {g.body}" for i, g in enumerate(knowledge_docs, start=1)}
    all_sources = {**sources, **knowledge}
    key = "exp:" + hashlib.sha256(json.dumps([sources, lang, llm.model, PROMPT_VERSION], sort_keys=True).encode()).hexdigest()
    cached = r.get(key)
    trace: dict = {"prompt_version": PROMPT_VERSION, "stages": [], "attempts": []}
    if cached:
        out = json.loads(cached)
        out["trace"] = {**out.get("trace", {}), "cache": "hit (explanation cache)"}
        return out
    system = SYSTEM.replace("{knowledge}", "\n".join(f"[{k}] {v}" for k, v in knowledge.items()))
    user = ("Sources:\n" + "\n".join(f"[{k}] {v}" for k, v in sources.items())
            + f"\n\nExplain the {cur.cycle} bill. Write in {LANGS[lang]}.")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    status, out = "UNVERIFIED", {}
    try:
        for attempt in range(3):
            res = llm.chat("explain" if attempt == 0 else "repair", messages, json_mode=True)
            trace["stages"].append(res.as_trace())
            try:
                out = json.loads(res.text)
            except json.JSONDecodeError:
                out, problems = {}, ["response was not valid JSON"]
            else:
                problems = _items_problems(out, all_sources)
            trace["attempts"].append({"attempt": attempt + 1, "verified": not problems, "problems": problems[:6]})
            if not problems:
                status = "VERIFIED" if attempt == 0 else "REPAIRED"
                break
            messages += [{"role": "assistant", "content": res.text},
                         {"role": "user", "content": "Fix these problems and return the full JSON again: " + "; ".join(problems[:8])}]
    except BudgetExceeded as e:
        status = "BUDGET"
        trace["degraded"] = [str(e)]
    stages = trace["stages"]
    trace["totals"] = {"cost_usd": round(sum(s["cost_usd"] for s in stages), 6), "llm_calls": len(stages),
                       "prompt_tokens": sum(s["prompt_tokens"] for s in stages),
                       "cache_hit_tokens": sum(s["cache_hit_tokens"] for s in stages)}
    cited = sorted({c for it in (out.get("drivers") or []) + (out.get("top_usage") or []) for c in it.get("sources", [])}
                   | set(out.get("tip_sources") or []), key=lambda x: (x[0], int(x[1:])))
    result = {"status": status, "explanation": out if status in ("VERIFIED", "REPAIRED") else None,
              "sources": [{"label": k, "body": all_sources[k]} for k in cited if k in all_sources]
              or [{"label": k, "body": v} for k, v in sources.items()],
              "trace": trace}
    if status in ("VERIFIED", "REPAIRED"):
        r.set(key, json.dumps(result), ex=30 * 86400)
    return result
