from datetime import UTC, datetime, timedelta

import pytest

from recruitment.core.time import ist_display
from recruitment.domain.retention import deadline


def test_rejection_deadline_uses_confirmation_time() -> None:
    active = datetime(2026, 10, 1, tzinfo=UTC)
    rejected = datetime(2026, 11, 3, tzinfo=UTC)
    assert deadline(active, rejected) == rejected + timedelta(days=7)


def test_other_deadline_and_business_override() -> None:
    now = datetime(2026, 11, 3, tzinfo=UTC)
    assert deadline(now, None) == now + timedelta(days=30)
    assert deadline(now, None, other_days=12) == now + timedelta(days=12)


def test_naive_timestamps_are_rejected() -> None:
    with pytest.raises(ValueError):
        deadline(datetime(2026, 11, 3), None)


def test_ist_display_converts_utc() -> None:
    assert ist_display(datetime(2026, 11, 3, 10, tzinfo=UTC)) == "03 Nov 2026, 03:30 PM IST"
