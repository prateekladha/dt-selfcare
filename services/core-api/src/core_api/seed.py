"""Provision the demo population: system-of-record rows + the aggregator's profile hashes."""
from __future__ import annotations

import os

from sqlalchemy import insert, select
from sqlalchemy.engine import Engine
from telco_common.demo import DEMO_SUBSCRIBERS
from telco_common.usage_state import UsageState

from .db import subscribers
from .security import hash_password


def seed_demo(engine: Engine, state: UsageState) -> int:
    password = os.environ.get("APP_DEMO_PASSWORD")
    if not password:
        raise RuntimeError("APP_DEMO_PASSWORD must be set to seed demo subscribers")
    created = 0
    with engine.begin() as cx:
        existing = {r[0] for r in cx.execute(select(subscribers.c.id))}
        for s in DEMO_SUBSCRIBERS:
            if s.subscriber_id not in existing:
                cx.execute(insert(subscribers).values(
                    id=s.subscriber_id, name=s.name, email=s.email, country=s.country, plan_code=s.plan_code,
                    password_hash=hash_password(password)))
                created += 1
            state.upsert_profile(s.subscriber_id, s.plan_code)  # idempotent
    return created
