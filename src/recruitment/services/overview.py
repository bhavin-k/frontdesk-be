"""Dashboard aggregates over retained records, using the same business RLS boundary."""

from collections import Counter
from datetime import date, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Date, cast, func, select

from recruitment.adapters.db.models import Application, CVUpload, Opening
from recruitment.core.time import utcnow
from recruitment.services.auth import Principal
from recruitment.services.common import missing
from recruitment.services.tenancy import TenancyService

COUNT_KEYS = ("cvs_received", "processed", "shortlisted", "needs_review", "rejected")


class OverviewService:
    def __init__(self, tenants: TenancyService) -> None:
        self.tenants = tenants

    def read(
        self, actor: Principal, tenant: UUID, opening_id: UUID | None, days: int
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            openings = list(
                session.scalars(select(Opening).order_by(Opening.created_at, Opening.id))
            )
            if opening_id and not any(row.id == opening_id for row in openings):
                raise missing()
            counts: dict[UUID, Counter[str]] = {row.id: Counter() for row in openings}
            for opening, status, attached, count in session.execute(
                select(
                    CVUpload.opening_id,
                    CVUpload.status,
                    CVUpload.application_id.is_not(None),
                    func.count(),
                ).group_by(
                    CVUpload.opening_id, CVUpload.status, CVUpload.application_id.is_not(None)
                )
            ):
                counts[opening]["cvs_received"] += count
                if status == "completed":
                    counts[opening]["processed"] += count
                if status == "needs_review" and not attached:
                    counts[opening]["needs_review"] += count
            for opening, status, count in session.execute(
                select(Application.opening_id, Application.status, func.count()).group_by(
                    Application.opening_id, Application.status
                )
            ):
                if status in {"shortlisted", "needs_review", "rejected"}:
                    counts[opening][status] += count
            chosen = [row for row in openings if not opening_id or row.id == opening_id]
            total: Counter[str] = Counter()
            for row in chosen:
                total.update(counts[row.id])
            today = utcnow().astimezone(ZoneInfo("Asia/Kolkata")).date()
            start = today - timedelta(days=days - 1)
            day = cast(func.timezone("Asia/Kolkata", CVUpload.source_received_at), Date)
            query = select(day, func.count()).where(day >= start, day <= today)
            if opening_id:
                query = query.where(CVUpload.opening_id == opening_id)
            daily: dict[date, int] = {
                key: count for key, count in session.execute(query.group_by(day))
            }
            return {
                "totals": {key: total[key] for key in COUNT_KEYS},
                "by_opening": [
                    {
                        "opening_id": row.id,
                        "title": row.title,
                        "counts": {key: counts[row.id][key] for key in COUNT_KEYS},
                    }
                    for row in chosen
                ],
                "daily_intake": [
                    {
                        "date": (start + timedelta(days=i)).isoformat(),
                        "cvs_received": daily.get(start + timedelta(days=i), 0),
                    }
                    for i in range(days)
                ],
                "timezone": "Asia/Kolkata",
                "scope": "retained_records",
                "days": days,
            }
