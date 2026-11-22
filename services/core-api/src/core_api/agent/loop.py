"""Assistant agent: multi-step, multi-tool ReAct loop behind the broker, streamed to the client.

  user question
    └─ turn n: LLM (stream) ──► text?  → stream tokens, then verify → done
                             └► tool calls → Broker.request (allow-list, JSON-Schema, scoped run, budget)
                                            → results appended as tool messages (labelled S#) → next turn
  limits: MAX_TURNS, MAX_TOOL_CALLS; on the last turn tools are withheld so the model must answer.
  verify: every number in the final answer must appear in the tool results it cites [S#];
          on failure the model is told exactly what is unsupported and repairs (≤2) WITH the tools, so it
          can look a fact up instead of dropping it — no templates. A repair that drops the facts is rejected.
Events (SSE): step · step_done · token · reset · repairing · replace · done · error
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import structlog
from sqlalchemy import insert

from ..db import agent_traces
from .agent import _CITE, numbers_in, verify
from .broker import Broker
from .llm import LLM, BudgetExceeded
from .tools import TOOLS, ToolContext, declarations

log = structlog.get_logger("agent.loop")
MAX_TURNS, MAX_TOOL_CALLS = 6, 10
PROMPT_VERSION = "assistant-tools-v2"

SYSTEM = """You are the billing assistant of a mobile operator. Answer the customer's question about their
bills, usage, plan, roaming and payments.

HOW TO WORK
- Use the tools to fetch facts; never answer from memory. Call several tools if the question needs them.
- Earlier messages in this chat are context only, not sources: look up again with the tools any fact you
  state in this answer, even if you mentioned it before.
- Never ask the customer for permission to look something up, and never say you don't have their details:
  fetch them with the tools.
- Every tool result has a "source_label" like S1. After each factual sentence cite the labels it uses, e.g. "... [S2]".
- Use only numbers that appear in the tool results you cite; keep amounts exactly as written. Don't calculate
  new numbers (days, differences, totals) unless a tool returned them (e.g. days_overdue).
- If the tools don't have the answer, say what is missing.
- If the customer wants more data, first check their current usage (get_current_usage), then you may prepare an
  add-on with propose_addon; if they want a different tariff, compare fees and data first, then propose_plan_change.
  Then tell them to approve it below. Never say it is bought, changed or active: it only happens after approval.
  Add-ons and tariff changes are charged on the next bill (no payment now).
- If the customer wants to pay, or a bill is overdue, or a purchase was refused because a bill is overdue, prepare
  the payment with propose_payment and tell them to pay in the secure checkout below. Never say it is paid.
- For greetings or small talk, reply briefly and offer what you can help with (bills, usage, plan, payments). No tools needed.

STYLE
- Lead with the direct answer in one sentence, then the reasons.
- Use short paragraphs or a bullet list ("- ") for several items. At most 120 words.
- Write like a person, not a database: dates in words ("15 November 2026"), "unpaid" / "nothing paid yet"
  rather than "0.00 EUR paid", and never copy field names (due_date, payment_status, days_overdue).
- Never mention tools, sources, labels, systems or that you are an AI (except the [S#] citations)."""

STEP_LABELS = {
    "list_bills": "Looking up your bills", "get_bill": "Reading your {cycle} bill", "compare_bills": "Comparing {cycle} with the previous month",
    "get_current_usage": "Checking your usage this month", "search_knowledge": "Searching your records and the tariff rules",
    "get_cdr_evidence": "Re-reading your raw usage records for {cycle}", "get_payments": "Checking your payments",
    "get_alerts": "Checking your alerts", "simulate_usage": "Pricing that scenario on your plan",
    "propose_addon": "Preparing an add-on for your approval",
    "propose_plan_change": "Preparing a tariff change for your approval",
    "propose_payment": "Preparing your payment",
}


def _label(tool: str, args: dict) -> str:
    tpl = STEP_LABELS.get(tool, tool)
    if "{cycle}" in tpl and args.get("cycle"):
        y, m = map(int, args["cycle"].split("-"))
        return tpl.format(cycle=datetime(y, m, 1).strftime("%B %Y"))
    return tpl.replace(" {cycle}", "")


SESSION_TURNS, SESSION_TTL_S = 6, 24 * 3600
# "...in the secure checkout below" / "approve it below" (not "you'll pay 10 EUR, below last month")
POINTS_BELOW = re.compile(r"\b(checkout|approve|approval|card)\b[^.\n]{0,60}\bbelow\b|\bbelow\b[^.\n]{0,30}\b(checkout|card)\b", re.IGNORECASE)


def load_history(r, sub: str, sid: str) -> list[dict]:
    return [json.loads(x) for x in r.lrange(f"chat:{sub}:{sid}", 0, SESSION_TURNS - 1)][::-1]


def save_turn(r, sub: str, sid: str, q: str, a: str) -> None:
    key = f"chat:{sub}:{sid}"
    pipe = r.pipeline()
    pipe.lpush(key, json.dumps({"q": q[:400], "a": _CITE.sub("", a)[:1200]}))
    pipe.ltrim(key, 0, SESSION_TURNS - 1)
    pipe.expire(key, SESSION_TTL_S)
    pipe.execute()


async def run_stream(question: str, ctx: ToolContext, llm: LLM, engine, lang: str = "en",
                     history: list[dict] | None = None) -> AsyncIterator[dict]:
    t0 = time.monotonic()
    trace = ctx.trace
    trace.update({"prompt_version": PROMPT_VERSION, "stages": [], "tool_calls": [], "attempts": []})
    broker = Broker(list(TOOLS))
    tools = declarations(list(TOOLS))
    today = datetime.now(UTC).strftime("%d %B %Y")
    from .prompts import get_prompt

    system_prompt, prompt_version = get_prompt("assistant-system", SYSTEM)  # versioned in Langfuse
    trace["prompt_version"] = f"{PROMPT_VERSION} ({prompt_version})"
    messages: list[dict] = [{"role": "system", "content": system_prompt}]  # stable prefix (+ tool decls) -> prefix cache
    for h in history or []:  # conversation memory: earlier questions and final answers (no tool payloads)
        messages += [{"role": "user", "content": h["q"]}, {"role": "assistant", "content": h["a"]}]
    messages.append({"role": "user", "content": f"Today is {today}. Reply in {'German' if lang == 'de' else 'Polish' if lang == 'pl' else 'English'}.\n\n{question}"})
    answer, status = "", "UNVERIFIED"
    st = {"calls": 0, "answer": "", "proposed": False}

    async def tool_loop(stream_tokens: bool):
        """LLM turns with tools until the model answers in text; the answer lands in st["answer"]."""
        st["answer"] = ""
        for turn in range(MAX_TURNS):
            allow_tools = turn < MAX_TURNS - 1 and st["calls"] < MAX_TOOL_CALLS
            streamed = False
            res = None
            async for kind, val in llm.astream("agent" if stream_tokens else "repair", messages, tools if allow_tools else None):
                if kind == "token":
                    if stream_tokens:
                        streamed = True
                        yield {"type": "token", "text": val}
                else:
                    res = val
            trace["stages"].append(res.as_trace())
            if res.tool_calls:
                if streamed:
                    yield {"type": "reset"}  # text that preceded a tool call is not the answer
                messages.append({"role": "assistant", "content": res.text or "", "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"] or "{}"}}
                    for c in res.tool_calls]})
                for c in res.tool_calls:
                    st["calls"] += 1
                    try:
                        args = json.loads(c["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    yield {"type": "step", "tool": c["name"], "label": _label(c["name"], args)}
                    out = await asyncio.to_thread(broker.request, c["name"], c["arguments"] or "{}", ctx)
                    yield {"type": "step_done", "tool": c["name"], "ok": out.decision != "DENY" and "error" not in out.result}
                    if out.decision == "PROPOSE" and out.result.get("proposal"):  # human-in-the-loop approval card
                        st["proposed"] = True
                        r_ = out.result
                        if r_.get("payment"):
                            yield {"type": "proposal", "kind": "PAYMENT", "purpose": r_["purpose"], "bill_id": r_["bill_id"],
                                   "cycle": r_["cycle"], "price": r_["amount"], "overdue": r_.get("overdue", False),
                                   "order_id": f"pay:{r_['purpose']}:{r_['cycle']}", "add_on": "", "extra_data": ""}
                        elif "new_tariff" in r_:
                            yield {"type": "proposal", "kind": "PLAN", "order_id": r_["order_id"], "add_on": r_["new_tariff"],
                                   "extra_data": f"{r_['current_tariff']} → {r_['new_tariff']} · data {r_['data']}",
                                   "price": r_["monthly_fee"].split(" -> ")[-1] + " / month"}
                        else:
                            yield {"type": "proposal", "kind": "ADDON", "order_id": r_["order_id"], "add_on": r_["add_on"],
                                   "extra_data": r_["extra_data"], "price": r_["price"]}
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out.result, default=str)[:3800]})
                continue
            st["answer"] = res.text
            return

    try:
        async for ev in tool_loop(stream_tokens=True):
            yield ev
        answer = st["answer"]
        first_had_numbers = bool(numbers_in(_CITE.sub("", answer)))

        for attempt in range(3):
            problems = verify(answer, dict(broker.sources)) if answer else ["empty answer"]
            if not problems and attempt > 0 and first_had_numbers and not _CITE.search(answer):
                # a "repair" that verifies only because it dropped every fact (e.g. "I don't have your
                # details") is not an answer to the customer's question
                problems = ["the rewrite dropped the facts the customer asked for; look them up with the tools and state them with citations"]
            if not st["proposed"] and POINTS_BELOW.search(answer):
                # the answer sends the customer to a checkout/approval card that was never prepared
                problems.append("you told the customer to pay or approve below, but nothing was prepared: call "
                                "propose_payment (or propose_addon / propose_plan_change) first, or don't mention it")
            trace["attempts"].append({"attempt": attempt + 1, "verified": not problems, "problems": problems[:6]})
            if not problems:
                status = ("VERIFIED" if attempt == 0 else "REPAIRED") if broker.sources else "CONVERSATIONAL"
                break
            if attempt == 2:
                break
            yield {"type": "repairing", "problems": problems[:4]}
            messages += [{"role": "assistant", "content": answer},
                         {"role": "user", "content": "Problems: " + "; ".join(problems[:6]) +
                          ". Answer the customer's question again. Look up every fact you state with the tools now (earlier"
                          " answers in this chat are not sources), use only numbers from those tool results and cite each"
                          " with its [S#]. Don't ask the customer anything and don't say you lack their details."}]
            async for ev in tool_loop(stream_tokens=False):  # the repair may call tools (steps are shown)
                if ev["type"] != "token":
                    yield ev
            answer = st["answer"]
        if trace["attempts"] and len(trace["attempts"]) > 1:
            yield {"type": "replace", "text": answer}  # one replacement with the checked answer
    except BudgetExceeded as e:
        status, answer = "BUDGET", "I can't answer right now because today's assistant budget has been used. Please try again tomorrow."
        trace.setdefault("degraded", []).append(str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("agent_failed", error=str(e)[:300])
        status, answer = "ERROR", "Something went wrong while answering. Please try again."
        trace.setdefault("degraded", []).append(f"{type(e).__name__}: {e}"[:300])

    if status == "UNVERIFIED":
        answer = "I couldn't put together an answer I can fully verify against your records. Try asking it differently."
        yield {"type": "replace", "text": answer}
    cited = sorted(set(_CITE.findall(answer)) & set(broker.sources), key=lambda x: int(x[1:]))
    stages = trace["stages"]
    trace["totals"] = {"cost_usd": round(sum(s["cost_usd"] for s in stages), 6),
                       "prompt_tokens": sum(s["prompt_tokens"] for s in stages),
                       "completion_tokens": sum(s["completion_tokens"] for s in stages),
                       "cache_hit_tokens": sum(s["cache_hit_tokens"] for s in stages), "llm_calls": len(stages),
                       "tool_calls": st["calls"]}
    pt = trace["totals"]["prompt_tokens"]
    trace["totals"]["cache_hit_ratio"] = round(trace["totals"]["cache_hit_tokens"] / pt, 3) if pt else None
    trace["latency_ms"] = int((time.monotonic() - t0) * 1000)
    citations = [broker.source_meta[lab] for lab in cited]
    def persist() -> None:
        with engine.begin() as cx:
            cx.execute(insert(agent_traces).values(id=str(uuid.uuid4()), subscriber_id=ctx.subscriber_id, question=question[:500],
                                                   status=status, answer=answer, trace=json.dumps(trace, default=str),
                                                   cost_usd=str(trace["totals"]["cost_usd"])))
    try:
        await asyncio.to_thread(persist)
    except Exception as e:  # noqa: BLE001
        log.warning("trace_persist_failed", error=str(e)[:200])
    yield {"type": "done", "status": status, "answer": answer, "citations": citations, "trace": trace}
