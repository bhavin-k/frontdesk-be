from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from recruitment.core.errors import ProviderError
from recruitment.domain.screening import WEEKDAYS, AvailabilityConfig, Slot, known_timezone


class ConfigAvailabilityProvider:
    def __init__(self, config: AvailabilityConfig) -> None:
        self.config = config
        self.duration = timedelta(minutes=config.slot_duration_minutes)
        self.buffer = timedelta(minutes=config.buffer_minutes)

    @classmethod
    def load(cls, path: Path) -> "ConfigAvailabilityProvider":
        try:
            return cls(AvailabilityConfig.model_validate_json(path.read_bytes()))
        except (OSError, ValidationError) as exc:
            raise ProviderError("SCREENING_AVAILABILITY_INVALID") from exc

    def timezone(self, tenant_timezone: str) -> str:
        return self.config.timezone or tenant_timezone

    def validate(self, start: datetime, tenant_timezone: str) -> Slot | None:
        zone = known_timezone(self.timezone(tenant_timezone))
        if zone is None:
            raise ProviderError("SCREENING_AVAILABILITY_INVALID")
        start = start.astimezone(UTC)
        local = start.astimezone(zone)
        end = start + self.duration
        local_end = end.astimezone(zone)
        if local.date() in self.config.blackout_dates or local_end.date() != local.date():
            return None
        hours = self.config.weekly_hours.get(WEEKDAYS[local.weekday()], [])
        if any(part.start <= local.time() < local_end.time() <= part.end for part in hours):
            return Slot(start, end, end + self.buffer)
        return None

    def slots(self, earliest: datetime, deadline: datetime, tenant_timezone: str) -> list[Slot]:
        zone = known_timezone(self.timezone(tenant_timezone))
        if zone is None:
            raise ProviderError("SCREENING_AVAILABILITY_INVALID")
        day = earliest.astimezone(zone).date()
        last = deadline.astimezone(zone).date()
        step = self.duration + self.buffer
        slots: list[Slot] = []
        while day <= last:
            if day not in self.config.blackout_dates:
                for part in self.config.weekly_hours.get(WEEKDAYS[day.weekday()], []):
                    start = datetime.combine(day, part.start, zone).astimezone(UTC)
                    end = datetime.combine(day, part.end, zone).astimezone(UTC)
                    while start + self.duration <= end and start <= deadline:
                        if start >= earliest and (slot := self.validate(start, tenant_timezone)):
                            slots.append(slot)
                        start += step
            day += timedelta(days=1)
        return sorted(slots, key=lambda slot: slot.start)
