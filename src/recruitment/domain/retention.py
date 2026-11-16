from datetime import datetime, timedelta


def deadline(
    last_activity: datetime,
    rejected_at: datetime | None,
    rejected_days: int = 7,
    other_days: int = 30,
) -> datetime:
    if last_activity.tzinfo is None or (rejected_at and rejected_at.tzinfo is None):
        raise ValueError("Retention requires aware timestamps.")
    return (
        (rejected_at + timedelta(days=rejected_days))
        if rejected_at
        else (last_activity + timedelta(days=other_days))
    )
