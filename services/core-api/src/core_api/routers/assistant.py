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
from ..agent.loop import run_stream
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
    """Async event stream with durable chat history (ADB): context = last turns of the session;
    the first turn of a session gets a title from the nano model."""
    import asyncio

    from ..agent import history as hist

    try:
        llm = build_llm(p.country, p.subscriber_id)
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    engine = get_engine()
    sid, is_new = await asyncio.to_thread(hist.ensure_session, engine, p.subscriber_id, body.session_id)
    import hashlib

    llm.meta = {"trace_name": "assistant", "session_id": sid,
                "trace_user_id": hashlib.sha256(p.subscriber_id.encode()).hexdigest()[:16]}  # pseudonymous
    history = await asyncio.to_thread(hist.recent_turns, engine, sid)
    ctx = ToolContext(p.subscriber_id, p.country, engine, get_usage_state(), get_embedder(), llm)
    yield {"type": "session", "session_id": sid, "turns": len(history)}
    async for ev in run_stream(body.question, ctx, llm, engine, body.lang, history):
        if ev["type"] == "done" and ev.get("answer"):
            await asyncio.to_thread(hist.append_turn, engine, sid, body.question, ev["answer"], ev["status"],
                                    ev.get("citations", []), ev.get("cards", []))
            ev["session_id"] = sid
            yield ev
            if is_new:
                title, source = await hist.make_title(llm, body.question, ev["answer"])
                await asyncio.to_thread(hist.set_title, engine, sid, title, source)
                yield {"type": "title", "session_id": sid, "title": title}
            continue
        yield ev


@router.post("/stream")
async def ask_stream(body: AskIn, p: Principal = Depends(current_principal)) -> StreamingResponse:
    """Multi-step tool-calling agent, streamed as Server-Sent Events (session, step, token, repairing, replace, done)."""
    _guard(p)

    async def sse():
        async for ev in _events(p, body):
            ev.pop("sources", None)   # all tool results: for /ask (evals), not for the browser
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
            "session_id": done.get("session_id"), "trace": done.get("trace", {}), "sources": done.get("sources", [])}


@router.get("/traces")
def traces(p: Principal = Depends(current_principal), limit: int = 10) -> list[dict]:
    with get_engine().connect() as cx:
        rows = cx.execute(select(agent_traces).where(agent_traces.c.subscriber_id == p.subscriber_id)
                          .order_by(agent_traces.c.created_at.desc()).limit(min(limit, 50))).all()
    return [{"id": r.id, "question": r.question, "status": r.status, "cost_usd": r.cost_usd,
             "created_at": r.created_at.isoformat(), "trace": json.loads(r.trace)} for r in rows]



@router.get("/sessions")
def sessions(p: Principal = Depends(current_principal)) -> list[dict]:
    from ..agent.history import list_sessions

    return list_sessions(get_engine(), p.subscriber_id)


@router.get("/sessions/{session_id}")
def session(session_id: str, p: Principal = Depends(current_principal)) -> dict:
    from ..agent.history import get_session

    s = get_session(get_engine(), p.subscriber_id, session_id)
    if s is None:
        raise HTTPException(404, "chat not found")
    return s


@router.delete("/sessions/{session_id}", status_code=204)
def delete_chat(session_id: str, p: Principal = Depends(current_principal)) -> Response:
    from ..agent.history import delete_session

    if not delete_session(get_engine(), p.subscriber_id, session_id):
        raise HTTPException(404, "chat not found")
    return Response(status_code=204)
