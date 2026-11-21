from datetime import UTC, datetime

from telco_common.codec import decode_records
from telco_common.models import Country, UsageBatch, UsageRecord, UsageType
from telco_common.partitioning import partition_for
from usage_collector.publisher import build_messages, publish


def batch(n: int, subs: int = 50) -> UsageBatch:
    t = datetime(2026, 11, 1, tzinfo=UTC)
    return UsageBatch(batch_id="pgw-01:000001", source="pgw-01", records=[
        UsageRecord(record_id=f"r-{i:08d}", subscriber_id=f"49151{i % subs:08d}", country=Country.DE,
                    usage_type=UsageType.DATA, quantity=i, event_time=t) for i in range(n)])


def test_records_land_on_their_subscriber_partition_and_nothing_is_lost():
    b = batch(4500, subs=7)
    msgs = build_messages(b, 4)
    assert sum(m.records for m in msgs) == 4500
    for m in msgs:
        assert {partition_for(r.subscriber_id, 4) for r in decode_records(m.payload)} == {m.partition}


def test_message_ids_are_stable_across_retries():
    assert [m.msg_id for m in build_messages(batch(300), 4)] == [m.msg_id for m in build_messages(batch(300), 4)]


class FakeProducer:
    def __init__(self, fail: bool = False):
        self.sent, self.fail = [], fail

    def produce(self, topic, value, partition, headers, on_delivery):
        self.sent.append((partition, headers["msg_id"]))
        on_delivery("broker down" if self.fail else None, None)

    def poll(self, timeout):
        return 0


def test_publish_reports_success_and_failure():
    msgs = build_messages(batch(100), 4)
    ok = FakeProducer()
    assert publish(ok, "usage-events", "b1", msgs) == [] and len(ok.sent) == len(msgs)
    errors = publish(FakeProducer(fail=True), "usage-events", "b1", msgs)
    assert errors and set(errors) == {"broker down"}
