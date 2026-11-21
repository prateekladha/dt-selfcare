"""Relational store (Autonomous DB in OCI) for the system-of-record data: subscribers, bills,
payments, notifications, AI explanation cache, and the transactional outbox.

Portable SQLAlchemy Core types only, so the same schema runs on Oracle ADB and on SQLite in tests.
JSON payloads are stored as text to stay portable across engines.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Identity,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.engine import Engine

metadata = MetaData()


def utcnow() -> datetime:
    return datetime.now(UTC)


subscribers = Table(
    "subscribers", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(128), nullable=False),
    Column("email", String(256), nullable=False),
    Column("country", String(2), nullable=False),
    Column("plan_code", String(32), nullable=False),
    Column("password_hash", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
)

bills = Table(
    "bills", metadata,
    Column("id", String(64), primary_key=True),  # "<subscriber>:<cycle>" - natural, idempotent
    Column("subscriber_id", String(32), nullable=False),
    Column("cycle", String(7), nullable=False),
    Column("plan_code", String(32), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("subtotal", BigInteger, nullable=False),
    Column("vat", BigInteger, nullable=False),
    Column("total", BigInteger, nullable=False),
    Column("amount_paid", BigInteger, nullable=False, default=0),
    Column("status", String(16), nullable=False),  # ISSUED | PAID
    Column("issued_at", DateTime(timezone=True), nullable=False),
    Column("due_date", DateTime(timezone=True), nullable=False),
    Column("payload", Text, nullable=False),  # full Bill JSON (lines + usage)
    UniqueConstraint("subscriber_id", "cycle", name="uq_bill_sub_cycle"),
)
Index("ix_bills_sub_cycle", bills.c.subscriber_id, bills.c.cycle)

payments = Table(
    "payments", metadata,
    Column("id", String(36), primary_key=True),
    Column("subscriber_id", String(32), nullable=False),
    Column("purpose", String(16), nullable=False),  # BILL | ADVANCE
    Column("bill_id", String(64)),
    Column("amount", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("gateway", String(16), nullable=False),
    Column("gateway_order_id", String(64), unique=True),
    Column("gateway_payment_id", String(64)),
    Column("status", String(16), nullable=False),  # CREATED | PAID | FAILED
    Column("idempotency_key", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
    Column("updated_at", DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False),
    UniqueConstraint("subscriber_id", "idempotency_key", name="uq_payment_idem"),
)

notifications = Table(
    "notifications", metadata,
    Column("id", Integer, Identity(), primary_key=True),
    Column("subscriber_id", String(32), nullable=False),
    Column("alert_id", String(96), nullable=False, unique=True),  # dedupe key from the pipeline
    Column("kind", String(32), nullable=False),
    Column("title", String(200), nullable=False),
    Column("body", String(1000), nullable=False),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
    Column("read_at", DateTime(timezone=True)),
)
Index("ix_notif_sub", notifications.c.subscriber_id, notifications.c.created_at)

explanations = Table(
    "explanations", metadata,
    Column("cache_key", String(64), primary_key=True),  # sha256(facts + lang + model)
    Column("subscriber_id", String(32), nullable=False),
    Column("cycle", String(7), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("text", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
)

outbox = Table(
    "outbox", metadata,
    Column("id", Integer, Identity(), primary_key=True),
    Column("topic", String(64), nullable=False),
    Column("event_key", String(64), nullable=False),
    Column("payload", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
    Column("published_at", DateTime(timezone=True)),
)

_engine: Engine | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine
    if _engine is None:
        from .settings import get_settings

        s = get_settings()
        kwargs: dict = {"pool_pre_ping": True}
        if url is None and s.db_dsn:
            from sqlalchemy.engine import URL

            url = URL.create("oracle+oracledb", username=s.db_user, password=s.db_password)
            kwargs |= {"connect_args": {"dsn": s.db_dsn}, "pool_size": 5, "max_overflow": 5, "pool_recycle": 1800}
        _engine = create_engine(url or s.database_url, **kwargs)
    return _engine


def set_engine(engine: Engine) -> None:
    global _engine
    _engine = engine


def create_schema(engine: Engine) -> None:
    metadata.create_all(engine)
