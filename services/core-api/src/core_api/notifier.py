"""notifier (worker deployable from the core-api image).

Loop 1 - usage alerts: `usage-alerts` topic -> notification row (unique alert_id => a user sees an
         alert once even if the relay re-published it) -> push to open apps via Valkey pub/sub.
Loop 2 - DB outbox relay: rows written in the same transaction as a payment -> `payment-events`.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

from confluent_kafka import Consumer, Producer
from prometheus_client import Counter, start_http_server
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError
from telco_common.catalog import OveragePolicy, get_plan
from telco_common.flags import init_flags
from telco_common.logging import configure_logging
from telco_common.streaming import StreamSettings

from .db import create_schema, get_engine, outbox, subscribers, utcnow
from .deps import get_valkey

log = configure_logging("notifier")
NOTIFIED = Counter("notifier_notifications_total", "Notifications created", ["level"])
DUPES = Counter("notifier_duplicates_total", "Duplicate alerts dropped")
OUTBOX = Counter("outbox_published_total", "Outbox events published")


def render(alert: dict, plan_code: str) -> tuple[str, str, str]:
    level = int(alert["level"])
    y, m = map(int, alert["cycle"].split("-"))
    month = datetime(y, m, 1, tzinfo=UTC).strftime("%B %Y")
    plan = get_plan(plan_code)
    gb = int(alert["allowance_bytes"]) / 1e9
    if level >= 100:
        if plan.overage_policy == OveragePolicy.THROTTLE:
            return ("DATA_EXHAUSTED", f"All your {month} data is used",
                    (f"You've used your {gb:.0f} GB for {month}. Speed is now reduced until your next cycle - "
                     "buy a data add-on to get full speed back."))
        return ("DATA_EXHAUSTED", f"All your {month} data is used",
                f"You've used your {gb:.0f} GB for {month}. Further data is charged per MB - consider a data add-on.")
    return (f"DATA_{level}", f"{level}% of your {month} data used",
            f"You've used {alert['pct']}% of your {gb:.0f} GB allowance for {month}.")


def alerts_loop(stop: threading.Event) -> None:
    stream = StreamSettings()
    engine, r = get_engine(), get_valkey()
    c = Consumer(stream.client_config() | {"group.id": "notifier", "enable.auto.commit": False,
                                           "auto.offset.reset": "earliest"})
    c.subscribe([stream.alerts_topic])
    while not stop.is_set():
        for m in c.consume(num_messages=100, timeout=1.0):
            if m.error():
                continue
            try:
                notify_alert(engine, r, json.loads(m.value()))
            except Exception as e:  # noqa: BLE001 - a DB blip or bad alert must not end the thread silently
                log.warning("alert_failed", error=str(e)[:200])
        c.commit(asynchronous=True)
    c.close()


def notify_alert(engine, r, alert: dict) -> None:
    if alert["cycle"] < datetime.now(UTC).strftime("%Y-%m"):
        return  # replay/backfill of a closed cycle: history, not something to notify now
    with engine.connect() as cx:
        plan_code = cx.execute(select(subscribers.c.plan_code)
                               .where(subscribers.c.id == alert["subscriber_id"])).scalar()
    if plan_code is None:
        return  # not a customer of this platform (e.g. load-test traffic)
    kind, title, body = render(alert, plan_code)
    try:
        with engine.begin() as cx:
            cx.execute(insert(notifications_table()).values(
                subscriber_id=alert["subscriber_id"], alert_id=alert["alert_id"], kind=kind,
                title=title, body=body))
    except IntegrityError:
        DUPES.inc()
        return
    from .services.home_cache import bump

    bump(r, alert["subscriber_id"])  # home read model: rebuild on next read
    r.publish(f"notif:{alert['subscriber_id']}", json.dumps({"kind": kind, "title": title, "body": body}))
    SPINE_DIRTY.add(alert["subscriber_id"])  # event-driven: re-embed this customer's context now
    NOTIFIED.labels(alert["level"]).inc()
    log.info("notified", alert_id=alert["alert_id"], kind=kind)


def notifications_table():
    from .db import notifications

    return notifications


def outbox_loop(stop: threading.Event) -> None:
    stream = StreamSettings()
    engine = get_engine()
    p = Producer(stream.client_config() | {"acks": "all"})
    while not stop.is_set():
        with engine.connect() as cx:
            rows = cx.execute(select(outbox).where(outbox.c.published_at.is_(None))
                              .order_by(outbox.c.id).limit(100)).all()
        for row in rows:
            p.produce(row.topic, key=row.event_key, value=row.payload.encode())  # payment-events, order-events
        if rows and p.flush(10) == 0:
            with engine.begin() as cx:
                cx.execute(update(outbox).where(outbox.c.id.in_([x.id for x in rows])).values(published_at=utcnow()))
            OUTBOX.inc(len(rows))
        stop.wait(2)


def billing_loop(stop: threading.Event) -> None:
    """Billing consumer of payment-events: applies PAYMENT_SUCCEEDED to bills exactly once."""
    from .services.billing_ledger import apply_payment_event

    stream = StreamSettings()
    engine = get_engine()
    c = Consumer(stream.client_config() | {"group.id": "billing", "enable.auto.commit": False,
                                           "auto.offset.reset": "earliest"})
    c.subscribe(["payment-events"])
    from confluent_kafka import TopicPartition

    while not stop.is_set():
        failed: set[int] = set()   # partitions with a failed event: nothing after it is processed or committed
        for m in c.consume(num_messages=100, timeout=1.0):
            if m.error() or m.partition() in failed:
                continue
            try:
                if apply_payment_event(engine, json.loads(m.value())):
                    log.info("payment_applied_to_bill")
                c.commit(message=m, asynchronous=False)   # commit only what was applied
            except Exception as e:  # noqa: BLE001 - e.g. DB down: re-read this event, never skip a payment
                log.warning("payment_apply_failed", error=str(e)[:200])
                failed.add(m.partition())
                c.seek(TopicPartition(m.topic(), m.partition(), m.offset()))
        if failed:
            stop.wait(2)
    c.close()


SPINE_DIRTY: set[str] = set()

ORDER_TEXT = {
    ("ADDON", "COMPLETED"): "Your data add-on is active", ("ADDON", "FAILED"): "Your add-on order couldn't be completed",
    ("PLAN", "COMPLETED"): "Your new tariff is active", ("PLAN", "FAILED"): "Your tariff change couldn't be completed",
}


def project_order_event(engine, r, ev: dict) -> bool:
    """order-events projection: home-cache invalidation, assistant context, and (terminal states) a
    notification + live push. Idempotent: notification alert_id = order:<id>:<status>."""
    from .db import notifications
    from .services.home_cache import bump

    sub = ev["subscriber_id"]
    bump(r, sub)
    SPINE_DIRTY.add(sub)
    title = ORDER_TEXT.get((ev.get("kind"), ev.get("status")))
    if title is None:
        return False
    try:
        with engine.begin() as cx:
            cx.execute(insert(notifications).values(subscriber_id=sub, alert_id=f"order:{ev['order_id']}:{ev['status']}",
                                                    kind=f"ORDER_{ev['status']}", title=title, body=ev.get("detail") or ""))
    except IntegrityError:
        return False  # redelivery
    bump(r, sub)
    r.publish(f"notif:{sub}", json.dumps({"kind": f"ORDER_{ev['status']}", "title": title, "body": ev.get("detail") or ""}))
    return True


def orders_loop(stop: threading.Event) -> None:
    """Consumer of `order-events` (group order-projections)."""
    stream = StreamSettings()
    engine, r = get_engine(), get_valkey()
    c = Consumer(stream.client_config() | {"group.id": "order-projections", "enable.auto.commit": False,
                                           "auto.offset.reset": "earliest"})
    c.subscribe(["order-events"])
    while not stop.is_set():
        for m in c.consume(num_messages=100, timeout=1.0):
            if m.error():
                continue
            try:
                ev = json.loads(m.value())
                if project_order_event(engine, r, ev):
                    log.info("order_notified", order=ev["order_id"][-6:], status=ev["status"])
            except Exception as e:  # noqa: BLE001
                log.warning("order_projection_failed", error=str(e)[:200])
        c.commit(asynchronous=True)
    c.close()


def reconcile_loop(stop: threading.Event) -> None:
    """Payment reconciler: every non-final payment is re-checked against the gateway's status API on a
    backoff schedule. Runs on startup too, so anything missed while the system was down is caught up."""
    from datetime import UTC, datetime

    from .services.gateway import Razorpay
    from .services.payments import due_for_check, reconcile_order
    from .settings import get_settings

    s = get_settings()
    if not s.razorpay_key_id:
        log.info("reconciler_disabled_no_gateway_keys")
        return
    gw = Razorpay(s.razorpay_key_id, s.razorpay_key_secret, s.razorpay_webhook_secret)
    engine = get_engine()
    from datetime import timedelta

    from sqlalchemy import update

    from .db import payments
    from .services.payments import apply_missing, next_delay_s

    while not stop.is_set():
        try:
            due = due_for_check(engine, datetime.now(UTC))
        except Exception as e:  # noqa: BLE001
            log.warning("reconcile_failed", error=str(e)[:200])
            due = []
        for order_id in due:   # one bad order must not stop the others from being checked
            try:
                res = reconcile_order(engine, gw, order_id, trigger="reconciler")
                log.info("payment_reconciled", order=order_id[-6:], result=res["result"])
            except Exception as e:  # noqa: BLE001 - back this order off instead of retrying it first every tick
                log.warning("reconcile_failed", order=order_id[-6:], error=str(e)[:200])
                with engine.begin() as cx:
                    cx.execute(update(payments).where(payments.c.gateway_order_id == order_id).values(
                        check_attempts=payments.c.check_attempts + 1,
                        next_check_at=datetime.now(UTC) + timedelta(seconds=next_delay_s(3))))
        try:
            if n := apply_missing(engine):   # safety net: captured bill payments that never reached the bill
                log.warning("payments_applied_by_sweep", count=n)
        except Exception as e:  # noqa: BLE001
            log.warning("apply_missing_failed", error=str(e)[:200])
        stop.wait(15)


def spine_loop(stop: threading.Event) -> None:
    """Layer 1 builder. Event-driven (alert -> refresh that subscriber immediately) plus a sweep every
    60 s that picks up new CDR totals, issued bills and payments. Content-hash upserts make an unchanged
    subscriber cost one DB read and zero embeddings."""
    from .agent.embed import get_embedder
    from .agent.spine import refresh_global, refresh_subscriber
    from .deps import get_usage_state

    engine, state = get_engine(), get_usage_state()
    emb = get_embedder()
    refresh_global(engine, emb)
    last_sweep = 0.0
    import time as _t

    while not stop.is_set():
        due = set(SPINE_DIRTY)
        SPINE_DIRTY.clear()
        if _t.monotonic() - last_sweep > 60:
            with engine.connect() as cx:
                due |= {r[0] for r in cx.execute(select(subscribers.c.id))}
            last_sweep = _t.monotonic()
        for sub in due:
            try:
                st = refresh_subscriber(engine, state, emb, sub)
                if st["embedded"]:
                    log.info("spine_refreshed", subscriber=sub[-4:], **st)
            except Exception as e:  # noqa: BLE001
                log.warning("spine_refresh_failed", error=str(e)[:200])
        stop.wait(5)


def main() -> None:
    init_flags()
    create_schema(get_engine())
    start_http_server(9000)
    stop = threading.Event()
    import signal

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    threads = [threading.Thread(target=f, args=(stop,), daemon=True) for f in (alerts_loop, outbox_loop, spine_loop, billing_loop, orders_loop)]
    for t in threads:
        t.start()
    log.info("notifier_started")
    for t in threads:
        t.join()
