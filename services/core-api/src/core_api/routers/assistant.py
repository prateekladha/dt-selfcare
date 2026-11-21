"""Ask-about-your-bill: the agent over the subscriber's context spine (L1-L4), with citations + trace."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from telco_common.flags import flag_bool

from ..agent.embed import get_embedder
from ..agent.factory import build_agent, build_llm
from ..agent.loop import load_history, run_stream, save_turn
from ..agent.tools import ToolContext
from ..db import agent_traces, get_engine
from ..deps import RateLimiter, get_usage_state, get_valkey
from ..security import Principal, current_principal
from ..settings import get_settings

router = APIRouter(prefix="/v1/me/assistant", tags=["assistant"])


class AskIn(BaseModel):
    question: str = Field(min_length=2, max_length=400)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{8,40}$")
    lang: str = Field(default="en", pattern="^(en|de|pl)$")
    cycle: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}$")


def run_agent(p: Principal, question: str, lang: str, cycle: str | None) -> dict:
    if not flag_bool("ai-explanations", p.country, p.subscriber_id):
        raise HTTPException(503, "the assistant is switched off")
    s = get_settings()
    if not RateLimiter(get_valkey()).allow(f"ask:{p.subscriber_id}", s.ai_daily_limit_per_subscriber * 10, 86400):  # abuse guard; spend is capped per stage
        raise HTTPException(429, "daily assistant limit reached")
    try:
        agent = build_agent(p.country, p.subscriber_id)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    return agent.ask(question, p.subscriber_id, lang, cycle).as_dict()


def _guard(p: Principal) -> None:
    if not flag_bool("ai-explanations", p.country, p.subscriber_id):
        raise HTTPException(503, "the assistant is switched off")
    s = get_settings()
    if not RateLimiter(get_valkey()).allow(f"ask:{p.subscriber_id}", s.ai_daily_limit_per_subscriber * 10, 86400):
        raise HTTPException(429, "daily assistant limit reached")


async def _events(p: Principal, body: AskIn):
    """Async event stream; conversation memory per session (last 6 turns, 24 h) in Valkey."""
    import uuid

    try:
        llm = build_llm(p.country, p.subscriber_id)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    sid = body.session_id or uuid.uuid4().hex[:16]
    import hashlib

    llm.meta = {"trace_name": "assistant", "session_id": sid,
                "trace_user_id": hashlib.sha256(p.subscriber_id.encode()).hexdigest()[:16]}  # pseudonymous
    r = get_valkey()
    history = load_history(r, p.subscriber_id, sid)
    ctx = ToolContext(p.subscriber_id, p.country, get_engine(), get_usage_state(), get_embedder(), llm)
    yield {"type": "session", "session_id": sid, "turns": len(history)}
    async for ev in run_stream(body.question, ctx, llm, get_engine(), body.lang, history):
        if ev["type"] == "done" and ev.get("answer"):
            save_turn(r, p.subscriber_id, sid, body.question, ev["answer"])
            ev["session_id"] = sid
        yield ev


@router.post("/stream")
async def ask_stream(body: AskIn, p: Principal = Depends(current_principal)) -> StreamingResponse:
    """Multi-step tool-calling agent, streamed as Server-Sent Events (session, step, token, repairing, replace, done)."""
    _guard(p)

    async def sse():
        async for ev in _events(p, body):
            yield f"data: {json.dumps(ev, default=str)}\n\n"

    return StreamingResponse(sse(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/ask")
async def ask(body: AskIn, response: Response, p: Principal = Depends(current_principal)) -> dict:
    """Same agent, non-streamed (returns the final 'done' event)."""
    response.headers["Cache-Control"] = "private, no-store"
    _guard(p)
    done: dict = {}
    async for ev in _events(p, body):
        if ev["type"] == "done":
            done = ev
    return {"status": done.get("status"), "answer": done.get("answer"), "citations": done.get("citations", []),
            "session_id": done.get("session_id"), "trace": done.get("trace", {})}


@router.get("/traces")
def traces(p: Principal = Depends(current_principal), limit: int = 10) -> list[dict]:
    with get_engine().connect() as cx:
        rows = cx.execute(select(agent_traces).where(agent_traces.c.subscriber_id == p.subscriber_id)
                          .order_by(agent_traces.c.created_at.desc()).limit(min(limit, 50))).all()
    return [{"id": r.id, "question": r.question, "status": r.status, "cost_usd": r.cost_usd,
             "created_at": r.created_at.isoformat(), "trace": json.loads(r.trace)} for r in rows]
