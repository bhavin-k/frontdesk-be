from datetime import UTC, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def utcnow() -> datetime:
    return datetime.now(UTC)


def ist_display(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("An aware timestamp is required.")
    return value.astimezone(IST).strftime("%d %b %Y, %I:%M %p IST")
