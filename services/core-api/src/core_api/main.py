"""core-api: modular monolith for the customer domains (billing, payments, insights, notifications).

Modules are separated by router/service packages with no cross-module DB access beyond their own
tables, so any module can be extracted into its own service when team size or load requires it.
"""
from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, make_asgi_app
from telco_common.flags import init_flags
from telco_common.logging import configure_logging

from .db import create_schema, get_engine
from .deps import get_usage_state, get_valkey
from .routers import admin, auth, events, me, payments, simulate, system
from .seed import seed_demo

log = configure_logging("core-api")
REQS = Counter("http_requests_total", "HTTP requests", ["route", "method", "status"])
LAT = Histogram("http_request_seconds", "HTTP latency", ["route", "method"])


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_flags()
    engine = get_engine()
    create_schema(engine)
    if os.getenv("APP_SEED_DEMO") == "true":
        log.info("demo_seeded", created=seed_demo(engine, get_usage_state()))
    yield


app = FastAPI(title="core-api", version="0.1.0", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())
for r in (auth.router, me.router, payments.router, events.router, admin.router, simulate.router, system.router):
    app.include_router(r)


@app.middleware("http")
async def observe(request: Request, call_next):
    rid = request.headers.get("x-request-id") or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=rid)
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled_error", path=request.url.path)
        response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
    route = request.scope.get("route")
    path = getattr(route, "path", "unmatched")
    LAT.labels(path, request.method).observe(time.perf_counter() - t0)
    REQS.labels(path, request.method, response.status_code).inc()
    response.headers["x-request-id"] = rid
    structlog.contextvars.clear_contextvars()
    return response


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> JSONResponse:
    checks = {}
    try:
        get_valkey().ping()
        checks["valkey"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["valkey"] = str(e)
    try:
        with get_engine().connect() as cx:
            cx.exec_driver_sql("SELECT 1 FROM DUAL" if get_engine().dialect.name == "oracle" else "SELECT 1")
        checks["db"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["db"] = str(e)
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ready" if ok else "degraded", **checks}, status_code=200 if ok else 503)


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_config=None,
                proxy_headers=True, forwarded_allow_ips="*")
