from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BOOKING_WINDOW = timedelta(hours=48)
MIN_NOTICE = timedelta(hours=2)
REPLY_LIMIT = 8
TERMINAL = {"NO_RESPONSE", "DECLINED", "CANCELLED", "FAILED_DELIVERY", "EMAIL_MISSING", "CLOSED"}
Weekday = Literal["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WEEKDAYS: tuple[Weekday, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class Hours(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: time
    end: time

    @model_validator(mode="after")
    def validate_range(self) -> "Hours":
        if self.start.tzinfo or self.end.tzinfo or self.start >= self.end:
            raise ValueError("Use increasing local working hours.")
        return self


def known_timezone(value: str | None) -> ZoneInfo | None:
    if not value:
        return None
    try:
        return ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return None


class AvailabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timezone: str | None = None
    weekly_hours: dict[Weekday, list[Hours]]
    slot_duration_minutes: int = Field(default=20, ge=1, le=240, strict=True)
    buffer_minutes: int = Field(default=10, ge=0, le=120, strict=True)
    blackout_dates: set[date] = Field(default_factory=set)

    @field_validator("timezone")
    @classmethod
    def validate_zone(cls, value: str | None) -> str | None:
        if value is not None and known_timezone(value) is None:
            raise ValueError("Use an IANA timezone.")
        return value

    @field_validator("weekly_hours")
    @classmethod
    def validate_intervals(cls, value: dict[Weekday, list[Hours]]) -> dict[Weekday, list[Hours]]:
        for day, hours in value.items():
            ordered = sorted(hours, key=lambda part: part.start)
            if any(a.end > b.start for a, b in zip(ordered, ordered[1:], strict=False)):
                raise ValueError("Working hours cannot overlap.")
            value[day] = ordered
        return value


@dataclass(frozen=True)
class Slot:
    start: datetime
    end: datetime
    blocked_until: datetime

    def overlaps(self, other: "Slot") -> bool:
        return self.start < other.blocked_until and other.start < self.blocked_until


class SlotPreference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    date: str | None = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    period: Literal["morning", "afternoon", "evening", "earlier", "later"] | None
    from_time: str | None = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    until_time: str | None = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    required: bool | None = None

    @model_validator(mode="after")
    def validate_bounds(self) -> "SlotPreference":
        if self.date:
            date.fromisoformat(self.date)
        if self.from_time and self.until_time and self.from_time >= self.until_time:
            raise ValueError("Use increasing local time bounds.")
        return self


class UnavailableWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    local_date: str | None = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
    from_time: str | None = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    until_time: str | None = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")

    @model_validator(mode="after")
    def validate_bounds(self) -> "UnavailableWindow":
        if self.local_date:
            date.fromisoformat(self.local_date)
        if not self.from_time and not self.until_time:
            raise ValueError("Use unavailable_dates for a whole day.")
        if self.from_time and self.until_time and self.from_time >= self.until_time:
            raise ValueError("Use increasing local time bounds.")
        return self

    def overlaps(self, slot: Slot, zone: ZoneInfo) -> bool:
        days = {slot.start.astimezone(zone).date(), slot.end.astimezone(zone).date()}
        if self.local_date:
            days.intersection_update({date.fromisoformat(self.local_date)})
        start_time = time.fromisoformat(self.from_time) if self.from_time else time.min
        for day in days:
            start = datetime.combine(day, start_time, zone).astimezone(UTC)
            end = (
                datetime.combine(day, time.fromisoformat(self.until_time), zone)
                if self.until_time
                else datetime.combine(day + timedelta(days=1), time.min, zone)
            ).astimezone(UTC)
            if slot.start < end and slot.end > start:
                return True
        return False


class ReplyIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: Literal[
        "ACCEPT_SLOT",
        "PROPOSE_TIME",
        "REQUEST_OPTIONS",
        "QUESTION",
        "ACKNOWLEDGE",
        "HANDOFF",
        "PROVIDE_DETAILS",
        "DECLINE",
        "CANCEL",
        "RESCHEDULE",
        "UNCLEAR",
    ]
    evidence: str = Field(max_length=500)
    slot_number: int | None = Field(ge=1, le=3)
    proposed_datetime: str | None = Field(max_length=40)
    timezone: str | None = Field(max_length=60)
    phone: str | None = Field(max_length=50)
    question: Literal["caller", "duration", "phone", "slots", "other"] | None
    preference: SlotPreference | None = None
    missing_detail: Literal["date", "time", "timezone"] | None = None
    unavailable_dates: list[Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]] = Field(
        default_factory=list, max_length=7
    )
    unavailable_windows: list[UnavailableWindow] = Field(default_factory=list, max_length=8)

    @field_validator("unavailable_dates")
    @classmethod
    def validate_unavailable_dates(cls, values: list[str]) -> list[str]:
        for value in values:
            date.fromisoformat(value)
        return values

    def permits(self, slot: Slot, zone: ZoneInfo) -> bool:
        first = slot.start.astimezone(zone).date().isoformat()
        last = (slot.end - timedelta(microseconds=1)).astimezone(zone).date().isoformat()
        return not (
            first in self.unavailable_dates
            or last in self.unavailable_dates
            or any(window.overlaps(slot, zone) for window in self.unavailable_windows)
        )


class ReplyDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    acknowledgment: str | None = Field(max_length=180)


def preference_distance(
    start: datetime, preference: SlotPreference, offered: list[datetime]
) -> int:
    """Rank local slot starts by how far they are from the requested date and hours."""
    minutes = start.hour * 60 + start.minute
    period_bounds = {"morning": (360, 720), "afternoon": (720, 1020), "evening": (1020, 1260)}
    lower, upper = period_bounds.get(preference.period or "", (0, 1440))
    if offered and preference.period == "later":
        lower = max(value.hour * 60 + value.minute for value in offered) + 1
    if offered and preference.period == "earlier":
        upper = min(value.hour * 60 + value.minute for value in offered)
    if preference.from_time or preference.until_time:
        lower, upper = 0, 1440
    if preference.from_time:
        value = time.fromisoformat(preference.from_time)
        lower = value.hour * 60 + value.minute
    if preference.until_time:
        value = time.fromisoformat(preference.until_time)
        upper = value.hour * 60 + value.minute
    days = abs((start.date() - date.fromisoformat(preference.date)).days) if preference.date else 0
    return days * 1440 + max(lower - minutes, minutes - upper + 1, 0)


def resolve_time(value: str | None, timezone: str | None) -> datetime | None:
    if not value or "T" not in value:
        return None
    try:
        local = datetime.fromisoformat(value)
    except ValueError:
        return None
    if local.tzinfo:
        return local.astimezone(UTC)
    zone = known_timezone(timezone)
    if zone is None:
        return None
    first, second = local.replace(tzinfo=zone, fold=0), local.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        # Both skipped and repeated local times require an explicit offset.
        return None
    return first.astimezone(UTC)
