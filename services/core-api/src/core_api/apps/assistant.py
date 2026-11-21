"""assistant-service: the tool-calling agent (SSE streaming) and its context spine reads.
Owns `context_docs` and `agent_traces`; scales on concurrent LLM streams, independently of billing."""
from __future__ import annotations

from contextlib import asynccontextmanager

from telco_common.flags import init_flags
from telco_common.logging import configure_logging

from ..db import create_schema, get_engine
from ..routers import assistant as assistant_router
from .common import build, serve

log = configure_logging("assistant-service")


@asynccontextmanager
async def lifespan(_):
    init_flags()
    create_schema(get_engine())
    yield


app = build("assistant-service", [assistant_router.router], lifespan)


def run() -> None:
    serve(app)
