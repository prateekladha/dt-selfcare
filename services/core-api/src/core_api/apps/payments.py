"""payments-service: checkout orders, confirmation, Razorpay webhook, reconciler.
Owns the `payments` table; communicates settlement to billing only via PAYMENT_SUCCEEDED events."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager

from telco_common.flags import init_flags
from telco_common.logging import configure_logging

from ..db import create_schema, get_engine
from ..routers import payments as payments_router
from .common import build, serve

log = configure_logging("payments-service")


@asynccontextmanager
async def lifespan(_):
    init_flags()
    create_schema(get_engine())
    from ..notifier import reconcile_loop

    stop = threading.Event()
    threading.Thread(target=reconcile_loop, args=(stop,), daemon=True).start()  # status-API reconciler
    yield
    stop.set()


app = build("payments-service", [payments_router.router], lifespan)


def run() -> None:
    serve(app)
