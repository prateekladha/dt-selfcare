"""System transparency API: live, read-only view of every layer (no black box).

Aggregates only, plus the *caller's own* data at each layer. No other customer's identifiers are
returned (alert events are filtered to the caller). Results are cached briefly per pod because
broker watermark lookups cost a network round trip.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from telco_common.flags import DEFAULTS, flag_bool, flag_obj, flag_str
from telco_common.models import cycle_of
from telco_common.partitioning import partition_for
from telco_common.streaming import StreamSettings

from ..db import bills, explanations, get_engine, metadata, notifications, outbox, payments, schema_migrations
from ..deps import get_usage_state, get_valkey
from ..security import Principal, current_principal
from ..services.resilience import BREAKER_OPEN
from ..settings import get_settings

router = APIRouter(prefix="/v1/system", tags=["system"])
log = structlog.get_logger("system")
_cache: dict[str, tuple[float, dict]] = {}
_locks: dict[str, threading.Lock] = {}


def _cached(key: str, ttl: float, fn):
    """Single flight per layer: while one request refreshes (the stream probe can block for seconds), the others
    get the last value instead of all running the same slow probe and exhausting the threadpool shared with
    login, bills and health checks."""
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    lock = _locks.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=hit is None, timeout=10 if hit is None else -1):
        return hit[1] if hit else {"error": "unavailable"}
    try:
        try:
            val = fn()
        except Exception as e:  # noqa: BLE001 - one broken layer must not hide the others
            log.warning("system_layer_failed", layer=key, error=f"{type(e).__name__}: {e}"[:300])
            val = {"error": "unavailable"}   # details in the server log, never in the page
        _cache[key] = (time.monotonic(), val)
        return val
    finally:
        lock.release()


def _stream() -> dict:
    from confluent_kafka import Consumer, TopicPartition

    st, state = StreamSettings(), get_usage_state()
    c = Consumer(st.client_config() | {"group.id": "system-probe", "enable.auto.commit": False,
                                       "socket.timeout.ms": 5000})
    try:
        parts = []
        for p in range(st.partitions):
            lo, hi = c.get_watermark_offsets(TopicPartition(st.usage_topic, p), timeout=5)
            applied = state.committed_offset(p)
            parts.append({"partition": p, "earliest": lo, "end": hi, "applied": applied,
                          "lag": max(0, hi - 1 - applied) if applied is not None else hi - lo,
                          "outbox_depth": state.r.xlen(f"alerts:{{p{p}}}")})
        topics = {}
        for t in (st.alerts_topic, "payment-events"):
            lo, hi = c.get_watermark_offsets(TopicPartition(t, 0), timeout=5)
            topics[t] = {"partitions": 1, "messages_retained": hi - lo, "end": hi}
    finally:
        c.close()
    return {"engine": "OCI Streaming (Kafka API, SASL_SSL)", "bootstrap": st.bootstrap.split(":")[0],
            "usage_topic": st.usage_topic, "partitions": parts, "topics": topics,
            "consumer_groups": ["usage-aggregator (state + offsets in Valkey)", "raw-archiver", "notifier"]}


def _valkey() -> dict:
    r = get_valkey()
    info = r.info()
    return {"engine": f"Valkey {info.get('valkey_version') or info.get('redis_version')}",
            "keys": r.dbsize(), "used_memory": info.get("used_memory_human"),
            "maxmemory_policy": info.get("maxmemory_policy"), "ops_per_sec": info.get("instantaneous_ops_per_sec"),
            "aof_enabled": bool(info.get("aof_enabled")), "uptime_s": info.get("uptime_in_seconds"),
            "connected_clients": info.get("connected_clients")}


def _db() -> dict:
    eng = get_engine()
    with eng.connect() as cx:
        counts = {t.name: cx.execute(select(func.count()).select_from(t)).scalar_one()
                  for t in metadata.sorted_tables}
        migrations = [r[0] for r in cx.execute(select(schema_migrations.c.id))]
        by_status = dict(cx.execute(select(payments.c.status, func.count()).group_by(payments.c.status)).all())
        providers = dict(cx.execute(select(explanations.c.provider, func.count()).group_by(explanations.c.provider)).all())
        backlog = cx.execute(select(func.count()).select_from(outbox).where(outbox.c.published_at.is_(None))).scalar_one()
    return {"engine": "Oracle Autonomous Database 23ai (Always Free)" if eng.dialect.name == "oracle" else eng.dialect.name,
            "schema_user": get_settings().db_user, "tables": counts, "migrations": migrations,
            "payments_by_status": by_status, "ai_cache_by_provider": providers, "outbox_unpublished": backlog}


@router.get("/overview")
def overview(p: Principal = Depends(current_principal)) -> dict:
    s = get_settings()
    st = StreamSettings()
    state = get_usage_state()
    part = partition_for(p.subscriber_id, st.partitions)
    cycle = cycle_of(datetime.now(UTC))
    ukey = f"u:{{p{part}}}:{p.subscriber_id}:{cycle}"
    raw = {k.decode(): v.decode() for k, v in state.r.hgetall(ukey).items()}
    eng = get_engine()
    with eng.connect() as cx:
        my_bills = [dict(r._mapping) for r in cx.execute(
            select(bills.c.id, bills.c.cycle, bills.c.total, bills.c.currency, bills.c.status, bills.c.amount_paid)
            .where(bills.c.subscriber_id == p.subscriber_id).order_by(bills.c.cycle.desc()).limit(4))]
        my_payments = [dict(r._mapping) for r in cx.execute(
            select(payments.c.id, payments.c.status, payments.c.amount, payments.c.currency, payments.c.charged_amount,
                   payments.c.charged_currency, payments.c.fx_rate, payments.c.gateway, payments.c.created_at)
            .where(payments.c.subscriber_id == p.subscriber_id).order_by(payments.c.created_at.desc()).limit(5))]
        my_notes = cx.execute(select(func.count()).select_from(notifications)
                              .where(notifications.c.subscriber_id == p.subscriber_id)).scalar_one()
    my_alerts = []
    for eid, fields in state.r.xrevrange(f"alerts:{{p{part}}}", count=200):
        f = {k.decode(): v.decode() for k, v in fields.items()}
        if f.get("subscriber_id") == p.subscriber_id:
            my_alerts.append({"id": eid.decode(), "level": f["level"], "cycle": f["cycle"], "pct": f["pct"]})
        if len(my_alerts) >= 6:
            break
    for row in my_payments:
        row["created_at"] = row["created_at"].isoformat()

    breakers = {}
    for metric in BREAKER_OPEN.collect():
        for sample in metric.samples:
            breakers[sample.labels["name"]] = "open" if sample.value else "closed"

    return {
        "as_of": datetime.now(UTC).isoformat(),
        "runtime": {"pod": os.getenv("HOSTNAME", "local"), "version": os.getenv("APP_VERSION", "dev")[:12],
                    "region": os.getenv("OCI_REGION", "ap-mumbai-1"), "cluster": "OKE basic · 1 × VM.Standard.E5.Flex"},
        "stream": _cached("stream", 15, _stream),
        "valkey": _cached("valkey", 5, _valkey),
        "database": _cached("db", 30, _db),   # ~15 COUNT(*)s: not on every page view
        "cdr": _cached("cdr", 10, lambda: {
            "by_type": state.cdr_type_stats(),
            "collector": {k.decode(): int(v) for k, v in state.r.hgetall("stats:collector").items()},
            # reasons only: raw entries carry other subscribers' record ids (which embed phone numbers) / batch ids
            "quarantine_sample": [{"reason": json.loads(x).get("reason", "")}
                                  for x in state.r.lrange("quarantine:emu-mediation-01", 0, 4)],
        }),
        "flags": _cached(f"flags:{p.country}:{p.subscriber_id}", 30, lambda: {
            k: (flag_bool(k, p.country, p.subscriber_id) if isinstance(v, bool)
                else flag_obj(k, p.country, p.subscriber_id) if isinstance(v, dict)
                else flag_str(k, p.country, p.subscriber_id)) for k, v in DEFAULTS.items()}),
        "breakers": breakers,
        "ai": {"providers_configured": [n for n, k in (("deepseek", s.deepseek_api_key), ("mistral", s.mistral_api_key),
                                                       ("selfhosted", s.selfhosted_llm_url)) if k],
               "daily_limit_per_subscriber": s.ai_daily_limit_per_subscriber},
        "payments": {"gateway": "razorpay (test mode)" if s.razorpay_key_id.startswith("rzp_test_") else "razorpay",
                     "webhook": f"{s.public_base_url}/v1/webhooks/razorpay"},
        "you": {"subscriber": p.subscriber_id, "partition": part, "valkey_usage_key": ukey,
                "valkey_usage_hash": raw, "valkey_profile": state.profile(p.subscriber_id),
                "applied_offset_of_your_partition": state.committed_offset(part), "recent_alert_events": my_alerts,
                "db_bills": my_bills, "db_payments": my_payments, "db_notifications": my_notes},
    }
