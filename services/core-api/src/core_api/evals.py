"""Offline evaluation of the assistant as a Langfuse experiment (dataset + task + evaluators).

  run-evals    upserts the golden dataset "billing-assistant-golden" and runs every item against the live
               assistant-service; scores land in Langfuse next to the traces (compare runs across prompt
               versions / models).
"""
from __future__ import annotations

import os

import httpx

DATASET = "billing-assistant-golden"
CORE = os.environ.get("EVAL_CORE", "http://core-api:8080")
ASSISTANT = os.environ.get("EVAL_ASSISTANT", "http://assistant-service:8080")
LINES = {"Jonas": "4915100000002", "Lena": "4915100000001", "Mia": "4915100000003", "Paul": "4915100000004",
         "Zofia": "48600000001"}

GOLDEN = [
    {"id": "jonas-oct-why", "who": "Jonas", "q": "Why did my October bill go up?", "must": ["roaming", "z2"], "tools": 1},
    {"id": "jonas-proof", "who": "Jonas", "q": "Can you prove my October bill from my usage records?", "must": ["cdr"], "tools": 1},
    {"id": "lena-data", "who": "Lena", "q": "Will my data last until the end of the month?", "must": ["mb"], "tools": 1},
    {"id": "lena-throttle", "who": "Lena", "q": "Will I be charged extra for going over my data?", "must": ["speed"], "tools": 1},
    {"id": "paul-extra", "who": "Paul", "q": "Did I pay anything beyond my plan fee last month?", "must": ["plan"], "tools": 1},
    {"id": "zofia-intl", "who": "Zofia", "q": "What drives my bill?", "must": ["international"], "tools": 1},
    {"id": "jonas-paid", "who": "Jonas", "q": "Have I paid my latest bill?", "must": ["bill"], "tools": 1},
    {"id": "greeting", "who": "Paul", "q": "Hello", "must": [], "tools": 0},
    {"id": "us-trip", "who": "Jonas", "q": "What would two weeks in the US cost me?", "must": ["us"], "tools": 1},
]

_tokens: dict[str, str] = {}


def _token(who: str) -> str:
    if who not in _tokens:
        r = httpx.post(f"{CORE}/v1/auth/login", json={"subscriber_id": LINES[who], "password": os.environ["APP_DEMO_PASSWORD"]},
                       timeout=20)
        r.raise_for_status()
        _tokens[who] = r.json()["access_token"]
    return _tokens[who]


def task(*, item, **_):
    data = item.input if hasattr(item, "input") else item["input"]
    r = httpx.post(f"{ASSISTANT}/v1/me/assistant/ask", headers={"Authorization": f"Bearer {_token(data['who'])}"},
                   json={"question": data["q"]}, timeout=120)
    r.raise_for_status()
    j = r.json()
    totals = (j.get("trace") or {}).get("totals", {})
    return {"answer": j.get("answer", ""), "status": j.get("status"), "citations": len(j.get("citations") or []),
            "tool_calls": totals.get("tool_calls", 0), "cost_usd": totals.get("cost_usd", 0.0)}


def grounded(*, output, **_):
    from langfuse import Evaluation

    ok = output["status"] in ("VERIFIED", "REPAIRED", "CONVERSATIONAL")
    return Evaluation(name="grounded", value=1.0 if ok else 0.0, comment=output["status"])


def mentions(*, output, expected_output, **_):
    from langfuse import Evaluation

    must = (expected_output or {}).get("must", [])
    if not must:
        return Evaluation(name="mentions", value=1.0)
    hit = sum(1 for m in must if m in output["answer"].lower())
    return Evaluation(name="mentions", value=hit / len(must), comment=f"{hit}/{len(must)}")


def used_tools(*, output, expected_output, **_):
    from langfuse import Evaluation

    need = (expected_output or {}).get("tools", 0)
    return Evaluation(name="used_tools", value=1.0 if output["tool_calls"] >= need else 0.0,
                      comment=f"{output['tool_calls']} tool calls")


def cost(*, output, **_):
    from langfuse import Evaluation

    return Evaluation(name="cost_usd", value=float(output["cost_usd"]))


def main() -> None:
    from langfuse import Langfuse

    lf = Langfuse()
    try:
        lf.get_dataset(DATASET)
    except Exception:  # noqa: BLE001 - first run
        lf.create_dataset(name=DATASET, description="Golden questions for the billing assistant (demo population)")
    for g in GOLDEN:
        lf.create_dataset_item(dataset_name=DATASET, id=g["id"], input={"who": g["who"], "q": g["q"]},
                               expected_output={"must": g["must"], "tools": g["tools"]})
    ds = lf.get_dataset(DATASET)
    res = lf.run_experiment(name="assistant-golden", description="live assistant-service", data=ds.items, task=task,
                            evaluators=[grounded, mentions, used_tools, cost], max_concurrency=2)
    print(res.format() if hasattr(res, "format") else res)
    lf.flush()
