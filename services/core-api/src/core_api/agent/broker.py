"""Tool broker: the ONLY component that executes tools (same contract as the perpetuum broker).

Every tool call from the model goes through `request()`:
  1. tool exists and is on the agent's allow-list           -> else DENY
  2. arguments validate against the tool's JSON Schema       -> else DENY (the model sees why and can fix it)
  3. execute (read tier) with the caller-scoped context
  4. result budgeted (item-level size cap) and labelled with a source id S# for citation
Errors become `{error}` results, never a crashed run. Every decision is recorded in the trace.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

import jsonschema

from .tools import TOOLS, ToolContext

RESULT_CHARS = 8000  # L4 item-level budget per tool result (~2k tokens); tools keep their output well below it
# (a cut result used to lose facts mid-JSON: the model missed them and the checker rejected correct figures)


@dataclass
class BrokerOutcome:
    decision: str        # EXECUTE | PROPOSE | DENY
    label: str | None    # S# source label for citation
    result: dict
    reason: str = ""


class Broker:
    def __init__(self, allowed: list[str]):
        self.allowed = set(allowed)
        self.sources: dict[str, str] = {}   # S# -> serialized result (verification corpus)
        self.source_meta: dict[str, dict] = {}

    def request(self, tool: str, raw_args: str | dict, ctx: ToolContext) -> BrokerOutcome:
        t0 = time.monotonic()
        log = ctx.trace.setdefault("tool_calls", [])
        if tool not in TOOLS or tool not in self.allowed:
            out = BrokerOutcome("DENY", None, {"denied": True, "reason": f"tool {tool!r} is not allowed"}, "not allowed")
            log.append({"tool": tool, "decision": "DENY", "reason": out.reason})
            return out
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
            jsonschema.validate(args, TOOLS[tool].parameters)
        except (json.JSONDecodeError, jsonschema.ValidationError) as e:
            reason = getattr(e, "message", str(e))[:200]
            log.append({"tool": tool, "decision": "DENY", "reason": f"invalid arguments: {reason}"})
            return BrokerOutcome("DENY", None, {"denied": True, "reason": f"invalid arguments: {reason}"}, reason)
        try:
            result = TOOLS[tool].run(args, ctx)
        except Exception as e:  # noqa: BLE001 - a failing tool returns an error result to the model
            result = {"error": f"{type(e).__name__}: {e}"[:300]}
        label = f"S{len(self.sources) + 1}"
        text = json.dumps(result, default=str)
        if len(text) > RESULT_CHARS:
            text = text[:RESULT_CHARS] + '…"(truncated)"'
        self.sources[label] = text
        self.source_meta[label] = {"label": label, "tool": tool, "args": args,
                                   "source_ref": result.get("source", tool) if isinstance(result, dict) else tool,
                                   "body": text}
        decision = "PROPOSE" if TOOLS[tool].tier == "propose" else "EXECUTE"
        log.append({"tool": tool, "args": args, "decision": decision, "label": label,
                    "ms": int((time.monotonic() - t0) * 1000), "error": result.get("error") if isinstance(result, dict) else None})
        return BrokerOutcome(decision, label, {"source_label": label, **result} if isinstance(result, dict) else {"source_label": label})
