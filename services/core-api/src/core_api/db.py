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
    Identity,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    select,
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
    Column("rated_counters", Text),  # the exact CDR totals this bill was priced from (late-usage adjustments)
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
    # What the gateway actually charged (may differ from the bill currency, e.g. INR for UPI)
    Column("charged_amount", BigInteger),
    Column("charged_currency", String(3)),
    Column("fx_rate", String(24)),
    Column("gateway", String(16), nullable=False),
    Column("gateway_order_id", String(64), unique=True),
    Column("gateway_payment_id", String(64)),
    Column("status", String(16), nullable=False),  # CREATED | PENDING | PAID | FAILED | EXPIRED
    Column("idempotency_key", String(64), nullable=False),
    # reconciliation (status always taken from the gateway's status API, never from webhook payloads)
    Column("gateway_status", String(24)),
    Column("check_attempts", Integer, nullable=False, default=0),
    Column("next_check_at", DateTime(timezone=True)),
    Column("last_checked_at", DateTime(timezone=True)),
    Column("settled_by", String(16)),
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


# ---- AI agent: Layer 1 durable context spine + run traces ----
context_docs = Table(
    "context_docs", metadata,
    Column("id", String(160), primary_key=True),        # deterministic: <subscriber|global>:<type>:<key>
    Column("subscriber_id", String(32)),                 # NULL = shared knowledge (tariffs, policies)
    Column("doc_type", String(32), nullable=False),
    Column("cycle", String(7)),
    Column("title", String(200), nullable=False),
    Column("body", Text, nullable=False),
    Column("source_ref", String(300), nullable=False),   # where the facts came from (bill id, Valkey key, ...)
    Column("content_hash", String(64), nullable=False),  # re-embed only when content changes
    Column("embedding_json", Text, nullable=False),      # portable copy; Oracle also has a native VECTOR column
    Column("updated_at", DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False),
)
Index("ix_ctx_sub", context_docs.c.subscriber_id, context_docs.c.doc_type)

agent_traces = Table(
    "agent_traces", metadata,
    Column("id", String(36), primary_key=True),
    Column("subscriber_id", String(32), nullable=False),
    Column("question", String(500), nullable=False),
    Column("status", String(24), nullable=False),        # VERIFIED | REPAIRED | UNVERIFIED | BUDGET | ERROR
    Column("answer", Text, nullable=False),
    Column("trace", Text, nullable=False),               # JSON: retrieval, grades, corrective actions, stages
    Column("cost_usd", String(24), nullable=False),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
)

# ---- tariff history (customer domain): drives pro-rating of fees and allowances ----
plan_changes = Table(
    "plan_changes", metadata,
    Column("order_id", String(36), primary_key=True),       # one change per order: a retried activity is a no-op
    Column("subscriber_id", String(32), nullable=False),
    Column("from_plan", String(32), nullable=False),
    Column("to_plan", String(32), nullable=False),
    Column("effective_at", DateTime(timezone=True), nullable=False),
)
Index("ix_plan_changes_sub", plan_changes.c.subscriber_id, plan_changes.c.effective_at)

# ---- assistant chat history (owned by assistant-service) ----
chat_sessions = Table(
    "chat_sessions", metadata,
    Column("id", String(32), primary_key=True),
    Column("subscriber_id", String(32), nullable=False),
    Column("title", String(120)),
    Column("title_source", String(32)),                    # title-llm model that wrote it, or "question"
    Column("turns", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
    Column("updated_at", DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False),
)
Index("ix_chat_sessions_sub", chat_sessions.c.subscriber_id, chat_sessions.c.updated_at)

chat_messages = Table(
    "chat_messages", metadata,
    Column("id", Integer, Identity(), primary_key=True),
    Column("session_id", String(32), nullable=False),
    Column("role", String(12), nullable=False),            # user | assistant
    Column("content", Text, nullable=False),
    Column("status", String(16)),                          # VERIFIED | REPAIRED | CONVERSATIONAL | ...
    Column("citations", Text),                             # JSON list of cited tool results
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
)
Index("ix_chat_messages_session", chat_messages.c.session_id, chat_messages.c.id)

# ---- billing ledger: payments applied to bills (owned by billing; fed by payment-events) ----
bill_payments = Table(
    "bill_payments", metadata,
    Column("payment_id", String(36), primary_key=True),   # idempotency: an event is applied once
    Column("bill_id", String(64), nullable=False),
    Column("amount", BigInteger, nullable=False),
    Column("applied_at", DateTime(timezone=True), default=utcnow, nullable=False),
)

# ---- write actions: orders (system of record) + the legacy BSS emulator's own table ----
orders = Table(
    "orders", metadata,
    Column("id", String(36), primary_key=True),               # also the Temporal workflow id (idempotent start)
    Column("subscriber_id", String(32), nullable=False),
    Column("kind", String(24), nullable=False),              # ADDON
    Column("addon_code", String(32)),
    Column("price", BigInteger, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("status", String(16), nullable=False),            # PROPOSED APPROVED SUBMITTED COMPLETED FAILED DECLINED
    Column("bss_order_id", String(40)),
    Column("detail", String(500)),
    Column("source", String(16), nullable=False),            # app | assistant
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
    Column("updated_at", DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False),
)
Index("ix_orders_sub", orders.c.subscriber_id, orders.c.created_at)

bss_orders = Table(  # owned by the simulated legacy BSS (stands in for a country's order system)
    "bss_orders", metadata,
    Column("id", String(40), primary_key=True),
    Column("order_id", String(36), nullable=False, unique=True),
    Column("status", String(16), nullable=False),            # SUBMITTED CONFIRMED REJECTED
    Column("reason", String(200)),
    Column("created_at", DateTime(timezone=True), default=utcnow, nullable=False),
    Column("processed_at", DateTime(timezone=True)),
)

schema_migrations = Table(
    "schema_migrations", metadata,
    Column("id", String(64), primary_key=True),
    Column("applied_at", DateTime(timezone=True), default=utcnow, nullable=False),
)

# Forward-only, idempotent migrations for tables that already exist (create_all never alters).
def _purge_backfill_notifications(cx) -> None:
    """Notifications created for already-closed cycles during the history backfill (before the notifier
    learned to skip closed cycles). Alert ids are '<subscriber>:<YYYY-MM>:<level>'."""
    from sqlalchemy import delete

    current = datetime.now(UTC).strftime("%Y-%m")
    rows = cx.execute(select(notifications.c.id, notifications.c.alert_id)).all()
    stale = [r.id for r in rows if r.alert_id.split(":")[1] < current]
    if stale:
        cx.execute(delete(notifications).where(notifications.c.id.in_(stale)))


# Forward-only, idempotent migrations: SQL strings (Oracle-only DDL) or callables (portable data fixes).
MIGRATIONS: list[tuple[str, list]] = [
    ("0002_payment_settlement_currency", [
        ("ALTER TABLE payments ADD (charged_amount NUMBER(19), charged_currency VARCHAR2(3 CHAR), "
        "fx_rate VARCHAR2(24 CHAR))"),
    ]),
    ("0003_purge_backfill_notifications", [_purge_backfill_notifications]),
    # Oracle 23ai AI Vector Search: native vector column for semantic retrieval (L2)
    ("0004_context_docs_vector", ["ALTER TABLE context_docs ADD (embedding VECTOR(384, FLOAT32))"]),
    ("0005_bills_rated_counters", ["ALTER TABLE bills ADD (rated_counters CLOB)"]),
    ("0006_payment_reconciliation", [
        ("ALTER TABLE payments ADD (gateway_status VARCHAR2(24 CHAR), check_attempts NUMBER(10) DEFAULT 0 NOT NULL, "
        "next_check_at TIMESTAMP WITH TIME ZONE, last_checked_at TIMESTAMP WITH TIME ZONE, settled_by VARCHAR2(16 CHAR))")]),
]


def create_schema(engine: Engine) -> None:
    from sqlalchemy import insert, inspect, select

    fresh = not inspect(engine).has_table("payments")
    metadata.create_all(engine)
    with engine.begin() as cx:
        done = {r[0] for r in cx.execute(select(schema_migrations.c.id))}
        for mid, statements in MIGRATIONS:
            if mid in done:
                continue
            for step in statements:
                if callable(step):
                    step(cx)
                elif engine.dialect.name == "oracle":
                    if mid == "0002_payment_settlement_currency" and (fresh or "charged_amount" in {
                            c["name"].lower() for c in inspect(cx).get_columns("payments")}):
                        continue
                    try:
                        cx.exec_driver_sql(step)
                    except Exception as e:
                        if "ORA-01430" not in str(e) and "ORA-00955" not in str(e):
                            raise
            cx.execute(insert(schema_migrations).values(id=mid))
