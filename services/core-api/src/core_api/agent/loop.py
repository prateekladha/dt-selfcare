"""Assistant agent: multi-step, multi-tool ReAct loop behind the broker, streamed to the client.

  user question
    └─ turn n: LLM (stream) ──► text?  → stream tokens, then verify → done
                             └► tool calls → Broker.request (allow-list, JSON-Schema, scoped run, budget)
                                            → results appended as tool messages (labelled S#) → next turn
  limits: MAX_TURNS, MAX_TOOL_CALLS; on the last turn tools are withheld so the model must answer.
  verify: every number in the final answer must appear in the tool results it cites [S#];
          on failure the model is told exactly what is unsupported and repairs (≤2) WITH the tools, so it
          can look a fact up instead of dropping it — no templates. A repair that drops the facts is rejected.
Events (SSE): step · step_done · proposal · token (only the final, checked answer) · done
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
from .agent import _CITE, _NUM, _norm, canon_spaced_numbers, numbers_in
from .broker import RESULT_CHARS, Broker
from .llm import LLM, BudgetExceeded
from .tools import TOOLS, ToolContext, declarations

log = structlog.get_logger("agent.loop")
MAX_TURNS, MAX_TOOL_CALLS = 6, 10
PROMPT_VERSION = "assistant-tools-v3"

SYSTEM = """You are the customer care assistant of a mobile operator, talking to one of its customers about
their bills, usage, plan, roaming and payments. Talk like a friendly, competent person at a service desk: warm,
calm, respectful and clear. Never sound like a system, a report or a developer.

GETTING THE FACTS RIGHT
- Think it through before you answer: gather everything the question needs first, then read every tool
  result, including refusals and blockers (e.g. a plan change refused because a bill is overdue), and build
  ONE consistent answer around them. Never offer something a tool just refused.
- Look facts up with the tools; never answer from memory. Earlier messages in this chat are context only:
  if you state a fact again, fetch it again in this turn.
- Use only figures the tools returned, written as they were returned. Don't work out new numbers (days,
  differences, totals); the tools already give things like days_overdue and outstanding.
- The customer's own bills, due dates, days left or days overdue, usage and payments ARE known: answer them
  straight from the tools ("There are no days left: it was due on 15 November and is now 7 days overdue").
- Only for policy the tools don't cover (grace periods, late fees, reminders, service restrictions) say you
  don't have those details and offer our care team. Never invent such rules, and never invent causes
  ("that may explain it") that the tools don't state.
- Never ask for permission to look something up, and never say you don't have the customer's details.
- If they want more data or a different plan, check their usage first, then prepare it (propose_addon /
  propose_plan_change) and tell them to approve it below. It only happens after they approve; it's charged on
  the next bill.
- If they want to pay, or a bill is overdue and paying is the next step, prepare it with propose_payment and
  say it plainly as a fact: "You can pay it in the secure checkout below." Never phrase it as an offer or a
  question ("I can get a payment ready if you'd like"): it is already there. Never say something is paid,
  bought or changed.

HOW TO TALK
- Answer exactly what was asked, first, in plain words. Usually 2 to 4 short sentences; a short list only when
  they asked about several things. Don't repeat details you already gave earlier in the chat unless asked.
- Everyday language: "your October bill of 41.59 EUR is unpaid", "was due on 15 November", "a payment that
  didn't go through". Never use internal words: status codes (ISSUED, CREATED, EXPIRED), field names, CDRs,
  "records system", "backend", "running totals", "tool", "source", or that you are an AI.
- Be human when it matters. If someone is worried, struggling to pay or annoyed, acknowledge it first ("I
  understand, bills can be stressful"), stay kind and never lecture. Explain their options honestly (paying
  now, a cheaper plan for the future, an add-on, or talking to our care team), and what happens next, gently.
- For greetings, thanks or small talk ("danke", "ok, thanks"), reply in one short sentence in their language,
  with no figures and without repeating an earlier answer, and offer help. End with at most one helpful next
  step or question, only when it's useful."""

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
_HAS_FIGURE = re.compile(r"\d[\d.,]*\d")   # 2+ digit figure (amounts, dates, MB)
HOLD_CHARS = 80   # ~a sentence: preambles before tool calls are shorter; an answer is visible within ~0.3 s
STREAM_DRAFTS = False   # the model's draft is never streamed: it may still be corrected by the checks


async def deliver(answer: str):
    """Write the final answer out in small chunks (reads like typing, ~1 s for a long answer). Only ever the
    checked answer, so nothing on screen is replaced afterwards."""
    parts = re.findall(r"\S+\s*", answer)
    step = max(2, len(parts) // 60)
    for i in range(0, len(parts), step):
        yield {"type": "token", "text": "".join(parts[i:i + step])}
        await asyncio.sleep(0.015)


def check_numbers(answer: str, labels: dict[str, str]) -> list[str]:
    """Every figure in the answer must appear in a tool result of THIS turn (earlier answers are not
    sources). The model doesn't have to place citation markers: attach_sources does that afterwards, so a
    correct answer is never rewritten just for a missing marker."""
    body = canon_spaced_numbers(_CITE.sub("", answer))
    supported = set().union(*(numbers_in(t) for t in labels.values())) if labels else set()
    bad = list(dict.fromkeys(t.strip() for t in _NUM.findall(body) if len(_norm(t)) > 1 and _norm(t) not in supported))
    if bad and not labels:
        return ["you stated figures without looking them up in this turn: fetch them with the tools"]
    return [f"the figure {t} is not in what the tools returned" for t in bad]


def attach_sources(answer: str, labels: dict[str, str]) -> str:
    """Citation markers placed by code: after each sentence with figures, the sources that contain them."""
    if not labels:
        return _CITE.sub("", answer)
    nums = {lab: numbers_in(t) for lab, t in labels.items()}
    order = sorted(labels, key=lambda x: int(x[1:]))

    def cite(sentence: str) -> str:
        found: list[str] = []
        for tok in _NUM.findall(canon_spaced_numbers(sentence)):
            n = _norm(tok)
            if len(n) < 2:
                continue
            lab = next((x for x in order if n in nums[x]), None)
            if lab and lab not in found:
                found.append(lab)
        if not found:
            return sentence
        tail = re.search(r"[.!?:]?\s*$", sentence)
        cut = tail.start() if tail else len(sentence)
        return sentence[:cut] + " " + "".join(f"[{x}]" for x in found) + sentence[cut:]

    lines = []
    for line in _CITE.sub("", answer).split("\n"):
        parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9*\"(])", line)
        lines.append(" ".join(cite(p) for p in parts))
    return re.sub(r"[ \t]+\[", " [", "\n".join(lines))


PAYMENT_WORDS = re.compile(r"\b(pay|payment|checkout)\b", re.IGNORECASE)
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
    cards: dict[str, dict] = {}   # kind -> approval card; sent only with the final answer, never twice

    def add_card(r_: dict) -> None:
        st["proposed"] = True
        if r_.get("payment"):
            cards["PAYMENT"] = {"type": "proposal", "kind": "PAYMENT", "purpose": r_["purpose"], "bill_id": r_["bill_id"],
                                "cycle": r_["cycle"], "price": r_["amount"], "overdue": r_.get("overdue", False),
                                "order_id": f"pay:{r_['purpose']}:{r_['cycle']}", "add_on": "", "extra_data": ""}
        elif "new_tariff" in r_:
            cards["PLAN"] = {"type": "proposal", "kind": "PLAN", "order_id": r_["order_id"], "add_on": r_["new_tariff"],
                             "extra_data": f"{r_['current_tariff']} → {r_['new_tariff']} · data {r_['data']}",
                             "price": r_["monthly_fee"].split(" -> ")[-1] + " / month"}
        else:
            cards["ADDON"] = {"type": "proposal", "kind": "ADDON", "order_id": r_["order_id"], "add_on": r_["add_on"],
                              "extra_data": r_["extra_data"], "price": r_["price"]}

    async def tool_loop(stream_tokens: bool):
        """LLM turns with tools until the model answers in text; the answer lands in st["answer"]."""
        st["answer"] = ""
        for turn in range(MAX_TURNS):
            allow_tools = turn < MAX_TURNS - 1 and st["calls"] < MAX_TOOL_CALLS
            streamed = False
            held = ""   # a turn's opening text is held back until it is clearly the answer, so a preamble
            res = None  # before a tool call ("Let me check your bills...") is never shown and then taken back
            # repairs are full tool loops too, so they share the agent stage's budget (the small "repair" cap
            # would otherwise turn repaired answers into budget messages); trace attempts tell them apart
            async for kind, val in llm.astream("agent", messages, tools if allow_tools else None):
                if kind == "token":
                    if not STREAM_DRAFTS or not stream_tokens:
                        continue   # drafts are never shown: only the final, checked answer is (see deliver)
                    if streamed:
                        yield {"type": "token", "text": val}
                    else:
                        held += val
                        # Until something was looked up in this turn, the whole reply is held: any figures in it would
                        # come from memory or earlier answers and be checked, rejected and visibly replaced. A reply
                        # without figures (greeting, clarifying question) is released at the end of the turn.
                        if len(held) >= HOLD_CHARS and st["calls"] > 0:
                            streamed = True
                            yield {"type": "token", "text": held}
                else:
                    res = val
            if STREAM_DRAFTS and stream_tokens and not streamed and held and not res.tool_calls and not (st["calls"] == 0 and _HAS_FIGURE.search(held)):
                streamed = True
                yield {"type": "token", "text": held}   # a short final answer: release it at once
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
                        add_card(out.result)   # held: sent with the final answer, one per kind
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out.result, default=str)[:RESULT_CHARS]})
                continue
            st["answer"] = res.text
            return

    try:
        async for ev in tool_loop(stream_tokens=True):
            yield ev
        answer = st["answer"]
        first_had_numbers = any(len(_norm(t)) > 1 for t in _NUM.findall(_CITE.sub("", answer)))

        for attempt in range(3):
            problems = check_numbers(answer, dict(broker.sources)) if answer else ["empty answer"]
            if (not problems and attempt > 0 and first_had_numbers
                    and not any(len(_norm(t)) > 1 for t in _NUM.findall(answer))):
                # a "repair" that verifies only because it dropped every fact (e.g. "I don't have your
                # details") is not an answer to the customer's question
                problems = ["the rewrite dropped the facts the customer asked for; look them up with the tools and state them with citations"]
            if not st["proposed"] and POINTS_BELOW.search(answer) and not PAYMENT_WORDS.search(answer):
                # the answer sends the customer to a checkout/approval card that was never prepared
                problems.append("you told the customer to pay or approve below, but nothing was prepared: call "
                                "propose_payment (or propose_addon / propose_plan_change) first, or don't mention it")
            trace["attempts"].append({"attempt": attempt + 1, "verified": not problems, "problems": problems[:6]})
            if not problems:
                status = ("VERIFIED" if attempt == 0 else "REPAIRED") if broker.sources else "CONVERSATIONAL"
                break
            if attempt == 2:
                break
            # silent: the customer sees the steps of any extra lookup, never a "checking" notice or a draft
            messages += [{"role": "assistant", "content": answer},
                         {"role": "user", "content": "Problems: " + "; ".join(problems[:6]) +
                          ". If the customer's message needs no facts (thanks, a greeting, small talk), reply in one short"
                          " sentence with no figures. Otherwise answer it again in the same warm, plain style. Look up every fact you"
                          " state with the tools now (earlier answers in this chat are not sources) and use only figures"
                          " from those tool results; fetch only what this question needs. Change only what is wrong."
                          " Don't ask the customer for permission and don't say you lack their details."}]
            async for ev in tool_loop(stream_tokens=False):  # the repair may call tools (steps are shown)
                if ev["type"] != "token":
                    yield ev
            answer = st["answer"]
        if status in ("VERIFIED", "REPAIRED"):
            answer = attach_sources(answer, dict(broker.sources))
            if not st["proposed"] and POINTS_BELOW.search(answer) and PAYMENT_WORDS.search(answer):
                # the answer sends the customer to the payment checkout: prepare it in code, no extra LLM round
                out = await asyncio.to_thread(broker.request, "propose_payment", '{"purpose": "bill"}', ctx)
                st["calls"] += 1
                if out.decision == "PROPOSE" and out.result.get("proposal"):
                    add_card(out.result)

    except BudgetExceeded as e:
        status, answer = "BUDGET", ("I'm sorry, I can't answer right now. Please try again a little later; your bills, "
                                    "usage and payments are all available in the app in the meantime.")
        trace.setdefault("degraded", []).append(str(e))
    except Exception as e:  # noqa: BLE001
        log.warning("agent_failed", error=str(e)[:300])
        status, answer = "ERROR", "I'm sorry, something went wrong on my side. Could you try asking again?"
        trace.setdefault("degraded", []).append(f"{type(e).__name__}: {e}"[:300])

    if status == "UNVERIFIED":
        answer = "I'm sorry, I couldn't put together an answer I can stand behind. Could you ask it a little differently?"
    if status in ("VERIFIED", "REPAIRED", "CONVERSATIONAL"):
        for card in cards.values():    # the approval cards that belong to the final answer
            yield card
    async for ev in deliver(answer):   # the facts are all in and checked: now the answer is written out
        yield ev
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
    sent = list(cards.values()) if status in ("VERIFIED", "REPAIRED", "CONVERSATIONAL") else []
    yield {"type": "done", "status": status, "answer": answer, "citations": citations, "cards": sent, "trace": trace}
