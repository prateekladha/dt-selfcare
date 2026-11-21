"""Durable chat history (Autonomous DB): sessions, messages, titles, retention.

Retention: the newest MAX_SESSIONS sessions per customer, none older than RETENTION_DAYS (pruned when a
new session starts). The last CONTEXT_TURNS turns of a session are sent to the model as context.
Titles: written once per session by a nano model through the gateway (alias `title-llm`, which falls
back to the primary model inside the gateway); if the gateway is down, the question itself, trimmed.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Engine

from ..db import chat_messages, chat_sessions

MAX_SESSIONS, RETENTION_DAYS, CONTEXT_TURNS = 50, 90, 6
_CITE = re.compile(r"\s*\[[DGS]\d{1,2}\]")


def ensure_session(engine: Engine, sub: str, session_id: str | None) -> tuple[str, bool]:
    if session_id:
        with engine.connect() as cx:
            if cx.execute(select(chat_sessions.c.id).where(chat_sessions.c.id == session_id,
                                                           chat_sessions.c.subscriber_id == sub)).first():
                return session_id, False
    sid = uuid.uuid4().hex
    with engine.begin() as cx:
        cx.execute(insert(chat_sessions).values(id=sid, subscriber_id=sub, turns=0))
    prune(engine, sub)
    return sid, True


def prune(engine: Engine, sub: str) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=RETENTION_DAYS)
    with engine.begin() as cx:
        ids = [r[0] for r in cx.execute(select(chat_sessions.c.id).where(chat_sessions.c.subscriber_id == sub)
                                        .order_by(chat_sessions.c.updated_at.desc()))]
        old = set(ids[MAX_SESSIONS:]) | {r[0] for r in cx.execute(select(chat_sessions.c.id).where(
            chat_sessions.c.subscriber_id == sub, chat_sessions.c.updated_at < cutoff))}
        if old:
            cx.execute(delete(chat_messages).where(chat_messages.c.session_id.in_(old)))
            cx.execute(delete(chat_sessions).where(chat_sessions.c.id.in_(old)))
    return len(old)


def recent_turns(engine: Engine, sid: str, n: int = CONTEXT_TURNS) -> list[dict]:
    with engine.connect() as cx:
        rows = cx.execute(select(chat_messages.c.role, chat_messages.c.content).where(chat_messages.c.session_id == sid)
                          .order_by(chat_messages.c.id.desc()).limit(n * 2)).all()[::-1]
    turns, q = [], None
    for role, content in rows:
        if role == "user":
            q = content
        elif q is not None:
            turns.append({"q": q, "a": _CITE.sub("", content)[:1200]})
            q = None
    return turns[-n:]


def append_turn(engine: Engine, sid: str, question: str, answer: str, status: str, citations: list) -> None:
    with engine.begin() as cx:
        cx.execute(insert(chat_messages).values(session_id=sid, role="user", content=question[:400]))
        cx.execute(insert(chat_messages).values(session_id=sid, role="assistant", content=answer, status=status,
                                                citations=json.dumps(citations, default=str)))
        cx.execute(update(chat_sessions).where(chat_sessions.c.id == sid).values(
            turns=chat_sessions.c.turns + 1, updated_at=datetime.now(UTC)))


def fallback_title(question: str) -> str:
    words = re.sub(r"\s+", " ", question).strip().rstrip("?.!").split(" ")
    t = " ".join(words[:7])
    return (t[:1].upper() + t[1:])[:60] or "New chat"


async def make_title(llm, question: str, answer: str) -> tuple[str, str]:
    """Nano model via the gateway (`title-llm`, falls back inside the gateway); question as last resort."""
    try:
        res = await llm.achat("title", [
            {"role": "system", "content": "Write a 3-6 word title for this billing chat. No quotes, no punctuation at the end."},
            {"role": "user", "content": f"Question: {question[:300]}\nAnswer: {_CITE.sub('', answer)[:300]}"}],
            model=llm.title_model)
        title = res.text.strip().strip('"').strip("'").rstrip(".")[:60]
        if 2 <= len(title):
            return title, res.model
    except Exception:  # noqa: BLE001, S110 - title is cosmetic; never fail the turn for it
        pass
    return fallback_title(question), "question"


def set_title(engine: Engine, sid: str, title: str, source: str) -> None:
    with engine.begin() as cx:
        cx.execute(update(chat_sessions).where(chat_sessions.c.id == sid, chat_sessions.c.title.is_(None))
                   .values(title=title, title_source=source))


def list_sessions(engine: Engine, sub: str) -> list[dict]:
    with engine.connect() as cx:
        rows = cx.execute(select(chat_sessions).where(chat_sessions.c.subscriber_id == sub, chat_sessions.c.turns > 0)
                          .order_by(chat_sessions.c.updated_at.desc()).limit(MAX_SESSIONS)).all()
    return [{"id": r.id, "title": r.title or "New chat", "turns": r.turns, "updated_at": r.updated_at.isoformat()} for r in rows]


def get_session(engine: Engine, sub: str, sid: str) -> dict | None:
    with engine.connect() as cx:
        s = cx.execute(select(chat_sessions).where(chat_sessions.c.id == sid, chat_sessions.c.subscriber_id == sub)).first()
        if s is None:
            return None
        msgs = cx.execute(select(chat_messages).where(chat_messages.c.session_id == sid).order_by(chat_messages.c.id)).all()
    return {"id": s.id, "title": s.title or "New chat", "messages": [
        {"role": m.role, "content": m.content, "status": m.status, "citations": json.loads(m.citations or "[]")} for m in msgs]}


def delete_session(engine: Engine, sub: str, sid: str) -> bool:
    with engine.begin() as cx:
        owned = cx.execute(select(func.count()).select_from(chat_sessions).where(
            chat_sessions.c.id == sid, chat_sessions.c.subscriber_id == sub)).scalar_one()
        if owned:
            cx.execute(delete(chat_messages).where(chat_messages.c.session_id == sid))
            cx.execute(delete(chat_sessions).where(chat_sessions.c.id == sid))
    return bool(owned)
