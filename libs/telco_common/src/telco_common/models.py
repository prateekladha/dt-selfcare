from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class Country(StrEnum):
    DE = "DE"
    PL = "PL"


class UsageType(StrEnum):
    DATA = "DATA"  # quantity = bytes
    VOICE = "VOICE"  # quantity = seconds
    SMS = "SMS"  # quantity = count
    ROAMING_DATA = "ROAMING_DATA"  # bytes, needs roaming_zone
    ROAMING_VOICE = "ROAMING_VOICE"  # seconds, needs roaming_zone
    INTL_VOICE = "INTL_VOICE"  # seconds, call to a foreign number from home network


class EventKind(StrEnum):
    USAGE = "USAGE"
    PLAN_CHANGED = "PLAN_CHANGED"  # add-on bought / plan switched mid-cycle


ROAMING_TYPES = {UsageType.ROAMING_DATA, UsageType.ROAMING_VOICE}
DATA_TYPES = {UsageType.DATA, UsageType.ROAMING_DATA}


class UsageRecord(BaseModel):
    """One mediated usage record (a CDR) as it enters the platform."""

    record_id: str = Field(min_length=8, max_length=128)
    subscriber_id: str = Field(min_length=3, max_length=64)
    country: Country
    kind: EventKind = EventKind.USAGE
    usage_type: UsageType | None = None
    quantity: int = Field(default=0, ge=0)
    roaming_zone: str | None = None
    # PLAN_CHANGED payload: catalogue code of the add-on bought (e.g. "DE-DATA-5GB").
    addon_code: str | None = None
    event_time: datetime

    @field_validator("event_time")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("event_time must be timezone-aware")
        return v.astimezone(UTC)

    def model_post_init(self, context: object, /) -> None:
        if self.kind == EventKind.USAGE:
            if self.usage_type is None:
                raise ValueError("usage_type is required for USAGE events")
            if self.usage_type in ROAMING_TYPES and not self.roaming_zone:
                raise ValueError("roaming_zone is required for roaming usage")
        elif self.kind == EventKind.PLAN_CHANGED and not self.addon_code:
            raise ValueError("addon_code is required for PLAN_CHANGED events")

    @property
    def cycle(self) -> str:
        """Billing cycle the record belongs to, by *event time* (not arrival time)."""
        return cycle_of(self.event_time)


def cycle_of(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m")


class UsageBatch(BaseModel):
    """A file/batch as delivered by a network mediation system.

    `batch_id` is unique per source (e.g. `pgw-fra-01:000184213`). Idempotency at the
    collector is per batch, which keeps the dedupe store tiny compared to per-record keys.
    """

    batch_id: str = Field(min_length=3, max_length=128)
    source: str = Field(min_length=1, max_length=64)
    records: list[UsageRecord] = Field(min_length=1, max_length=5000)
