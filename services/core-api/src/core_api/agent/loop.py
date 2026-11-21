"""Assistant agent: multi-step, multi-tool ReAct loop behind the broker, streamed to the client.

  user question
    └─ turn n: LLM (stream) ──► text?  → stream tokens, then verify → done
                             └► tool calls → Broker.request (allow-list, JSON-Schema, scoped run, budget)
                                            → results appended as tool messages (labelled S#) → next turn
  limits: MAX_TURNS, MAX_TOOL_CALLS; on the last turn tools are withheld so the model must answer.
  verify: every number in the final answer must appear in the tool results it cites [S#];
          on failure the model is told exactly what is unsupported and repairs (≤2) — no templates.
Events (SSE): step · step_done · token · reset · repairing · replace · done · error
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import structlog
from sqlalchemy import insert

from ..db import agent_traces
from .agent import _CITE, verify
from .broker import Broker
from .llm import LLM, BudgetExceeded
from .tools import TOOLS, ToolContext, declarations

log = structlog.get_logger("agent.loop")
MAX_TURNS, MAX_TOOL_CALLS = 6, 10
PROMPT_VERSION = "assistant-tools-v1"

SYSTEM = """You are the billing assistant of a mobile operator. Answer the customer's question about their
bills, usage, plan, roaming and payments.

HOW TO WORK
- Use the tools to fetch facts; never answer from memory. Call several tools if the question needs them.
- Every tool result has a "source_label" like S1. After each factual sentence cite the labels it uses, e.g. "... [S2]".
- Use only numbers that appear in the tool results you cite; keep amounts exactly as written.
- If the tools don't have the answer, say what is missing.
- For greetings or small talk, reply briefly and offer what you can help with (bills, usage, plan, payments). No tools needed.

STYLE
- Lead with the direct answer in one sentence, then the reasons.
- Use short paragraphs or a bullet list ("- ") for several items. At most 120 words.
- Never mention tools, sources, labels, systems or that you are an AI (except the [S#] citations)."""

STEP_LABELS = {
    "list_bills": "Looking up your bills", "get_bill": "Reading your {cycle} bill", "compare_bills": "Comparing {cycle} with the previous month",
    "get_current_usage": "Checking your usage this month", "search_knowledge": "Searching your records and the tariff rules",
    "get_cdr_evidence": "Re-reading your raw usage records for {cycle}", "get_payments": "Checking your payments",
    "get_alerts": "Checking your alerts", "simulate_usage": "Pricing that scenario on your plan",
}


def _label(tool: str, args: dict) -> str:
    tpl = STEP_LABELS.get(tool, tool)
    if "{cycle}" in tpl and args.get("cycle"):
        y, m = map(int, args["cycle"].split("-"))
        return tpl.format(cycle=datetime(y, m, 1).strftime("%B %Y"))
    return tpl.replace(" {cycle}", "")


def run_stream(question: str, ctx: ToolContext, llm: LLM, engine, lang: str = "en") -> Iterator[dict]:
    t0 = time.monotonic()
    trace = ctx.trace
    trace.update({"prompt_version": PROMPT_VERSION, "stages": [], "tool_calls": [], "attempts": []})
    broker = Broker(list(TOOLS))
    tools = declarations(list(TOOLS))
    today = datetime.now(UTC).strftime("%d %B %Y")
    messages: list[dict] = [{"role": "system", "content": SYSTEM},  # stable prefix (+ tool decls) -> prefix cache
                            {"role": "user", "content": f"Today is {today}. Reply in {'German' if lang == 'de' else 'Polish' if lang == 'pl' else 'English'}.\n\n{question}"}]
    answer, status, calls = "", "UNVERIFIED", 0
    try:
        for turn in range(MAX_TURNS):
            allow_tools = turn < MAX_TURNS - 1 and calls < MAX_TOOL_CALLS
            streamed = False
            res = None
            for kind, val in llm.stream("agent", messages, tools if allow_tools else None):
                if kind == "token":
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
                    calls += 1
                    try:
                        args = json.loads(c["arguments"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    yield {"type": "step", "tool": c["name"], "label": _label(c["name"], args)}
                    out = broker.request(c["name"], c["arguments"] or "{}", ctx)
                    yield {"type": "step_done", "tool": c["name"], "ok": out.decision == "EXECUTE" and "error" not in out.result}
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out.result, default=str)[:3800]})
                continue
            answer = res.text
            break

        labels = dict(broker.sources)
        for attempt in range(3):
            problems = verify(answer, labels) if answer else ["empty answer"]
            trace["attempts"].append({"attempt": attempt + 1, "verified": not problems, "problems": problems[:6]})
            if not problems:
                status = ("VERIFIED" if attempt == 0 else "REPAIRED") if broker.sources else "CONVERSATIONAL"
                break
            if attempt == 2:
                break
            yield {"type": "repairing", "problems": problems[:4]}
            messages += [{"role": "assistant", "content": answer},
                         {"role": "user", "content": "Problems: " + "; ".join(problems[:6]) +
                          ". Rewrite the answer so every number appears in the tool results you cite, with [S#] citations."}]
            rep = llm.chat("repair", messages)
            trace["stages"].append(rep.as_trace())
            answer = rep.text
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
                       "tool_calls": calls}
    pt = trace["totals"]["prompt_tokens"]
    trace["totals"]["cache_hit_ratio"] = round(trace["totals"]["cache_hit_tokens"] / pt, 3) if pt else None
    trace["latency_ms"] = int((time.monotonic() - t0) * 1000)
    citations = [broker.source_meta[lab] for lab in cited]
    try:
        with engine.begin() as cx:
            cx.execute(insert(agent_traces).values(id=str(uuid.uuid4()), subscriber_id=ctx.subscriber_id, question=question[:500],
                                                   status=status, answer=answer, trace=json.dumps(trace, default=str),
                                                   cost_usd=str(trace["totals"]["cost_usd"])))
    except Exception as e:  # noqa: BLE001
        log.warning("trace_persist_failed", error=str(e)[:200])
    yield {"type": "done", "status": status, "answer": answer, "citations": citations, "trace": trace}
