"""Ask-about-your-bill: the agent over the subscriber's context spine (L1-L4), with citations + trace."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from telco_common.flags import flag_bool

from ..agent.factory import build_agent
from ..db import agent_traces, get_engine
from ..deps import RateLimiter, get_valkey
from ..security import Principal, current_principal
from ..settings import get_settings

router = APIRouter(prefix="/v1/me/assistant", tags=["assistant"])


class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=400)
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


@router.post("/ask")
def ask(body: AskIn, response: Response, p: Principal = Depends(current_principal)) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    return run_agent(p, body.question, body.lang, body.cycle)


@router.get("/traces")
def traces(p: Principal = Depends(current_principal), limit: int = 10) -> list[dict]:
    with get_engine().connect() as cx:
        rows = cx.execute(select(agent_traces).where(agent_traces.c.subscriber_id == p.subscriber_id)
                          .order_by(agent_traces.c.created_at.desc()).limit(min(limit, 50))).all()
    return [{"id": r.id, "question": r.question, "status": r.status, "cost_usd": r.cost_usd,
             "created_at": r.created_at.isoformat(), "trace": json.loads(r.trace)} for r in rows]
