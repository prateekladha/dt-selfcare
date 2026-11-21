"""Shared app plumbing for each service: health, readiness, metrics, request ids."""
from __future__ import annotations

import os
import time
import uuid

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Histogram, make_asgi_app

REQS = Counter("svc_http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
LAT = Histogram("svc_http_request_seconds", "HTTP latency", ["service", "route", "method"])


def build(name: str, routers: list, lifespan=None) -> FastAPI:
    app = FastAPI(title=name, version="0.1.0", lifespan=lifespan)
    app.mount("/metrics", make_asgi_app())
    for r in routers:
        app.include_router(r)

    @app.middleware("http")
    async def observe(request: Request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(request_id=rid, service=name)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - last-resort handler returns a request id instead of a stack trace
            structlog.get_logger(name).exception("unhandled_error", path=request.url.path)
            response = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)
        path = getattr(request.scope.get("route"), "path", "unmatched")
        LAT.labels(name, path, request.method).observe(time.perf_counter() - t0)
        REQS.labels(name, path, request.method, response.status_code).inc()
        response.headers["x-request-id"] = rid
        structlog.contextvars.clear_contextvars()
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "service": name}

    @app.get("/readyz")
    def readyz() -> dict:
        from ..db import get_engine
        from ..deps import get_valkey

        get_valkey().ping()
        with get_engine().connect() as cx:
            cx.exec_driver_sql("SELECT 1 FROM DUAL" if get_engine().dialect.name == "oracle" else "SELECT 1")
        return {"status": "ready", "service": name}

    return app


def serve(app: FastAPI) -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_config=None,
                proxy_headers=True, forwarded_allow_ips="*")
