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
    plan = get_plan(plan_code)
    gb = int(alert["allowance_bytes"]) / 1e9
    if level >= 100:
        if plan.overage_policy == OveragePolicy.THROTTLE:
            return ("DATA_EXHAUSTED", "You've used all your data",
                    (f"You've used your {gb:.0f} GB. Speed is now reduced until your next cycle - "
                     "buy a data add-on to get full speed back."))
        return ("DATA_EXHAUSTED", "You've used all your data",
                f"You've used your {gb:.0f} GB. Further data is charged per MB - consider a data add-on.")
    return (f"DATA_{level}", f"You've used {level}% of your data",
            f"You've used {level}% of your {gb:.0f} GB data allowance this cycle.")


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
            alert = json.loads(m.value())
            if alert["cycle"] < datetime.now(UTC).strftime("%Y-%m"):
                continue  # replay/backfill of a closed cycle: history, not something to notify now
            with engine.connect() as cx:
                plan_code = cx.execute(select(subscribers.c.plan_code)
                                       .where(subscribers.c.id == alert["subscriber_id"])).scalar()
            if plan_code is None:
                continue  # not a customer of this platform (e.g. load-test traffic)
            kind, title, body = render(alert, plan_code)
            try:
                with engine.begin() as cx:
                    cx.execute(insert(notifications_table()).values(
                        subscriber_id=alert["subscriber_id"], alert_id=alert["alert_id"], kind=kind,
                        title=title, body=body))
            except IntegrityError:
                DUPES.inc()
                continue
            r.publish(f"notif:{alert['subscriber_id']}", json.dumps({"kind": kind, "title": title, "body": body}))
            NOTIFIED.labels(alert["level"]).inc()
            log.info("notified", alert_id=alert["alert_id"], kind=kind)
        c.commit(asynchronous=True)
    c.close()


def notifications_table():
    from .db import notifications

    return notifications


def outbox_loop(stop: threading.Event) -> None:
    stream = StreamSettings()
    engine = get_engine()
    p = Producer(stream.client_config() | {"acks": "all"})
    topic = "payment-events"
    while not stop.is_set():
        with engine.connect() as cx:
            rows = cx.execute(select(outbox).where(outbox.c.published_at.is_(None))
                              .order_by(outbox.c.id).limit(100)).all()
        for row in rows:
            p.produce(topic, key=row.event_key, value=row.payload.encode())
        if rows and p.flush(10) == 0:
            with engine.begin() as cx:
                cx.execute(update(outbox).where(outbox.c.id.in_([x.id for x in rows])).values(published_at=utcnow()))
            OUTBOX.inc(len(rows))
        stop.wait(2)


def main() -> None:
    init_flags()
    create_schema(get_engine())
    start_http_server(9000)
    stop = threading.Event()
    import signal

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    threads = [threading.Thread(target=f, args=(stop,), daemon=True) for f in (alerts_loop, outbox_loop)]
    for t in threads:
        t.start()
    log.info("notifier_started")
    for t in threads:
        t.join()
