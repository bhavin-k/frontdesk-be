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
            cv_counts = (
                select(
                    CVUpload.opening_id,
                    func.count().label("received"),
                    func.count().filter(CVUpload.status == "completed").label("processed"),
                    func.count()
                    .filter(CVUpload.status == "needs_review", CVUpload.application_id.is_(None))
                    .label("review"),
                )
                .group_by(CVUpload.opening_id)
                .subquery()
            )
            app_counts = (
                select(
                    Application.opening_id,
                    func.count().filter(Application.status == "shortlisted").label("shortlisted"),
                    func.count().filter(Application.status == "needs_review").label("review"),
                    func.count().filter(Application.status == "rejected").label("rejected"),
                )
                .group_by(Application.opening_id)
                .subquery()
            )
            rows = list(
                session.execute(
                    select(
                        Opening,
                        cv_counts.c.received,
                        cv_counts.c.processed,
                        cv_counts.c.review,
                        app_counts.c.shortlisted,
                        app_counts.c.review,
                        app_counts.c.rejected,
                    )
                    .outerjoin(cv_counts, cv_counts.c.opening_id == Opening.id)
                    .outerjoin(app_counts, app_counts.c.opening_id == Opening.id)
                    .order_by(Opening.created_at, Opening.id)
                )
            )
            openings = [row[0] for row in rows]
            if opening_id and not any(row.id == opening_id for row in openings):
                raise missing()
            counts = {
                opening.id: Counter(
                    {
                        "cvs_received": received or 0,
                        "processed": processed or 0,
                        "needs_review": (cv_review or 0) + (app_review or 0),
                        "shortlisted": shortlisted or 0,
                        "rejected": rejected or 0,
                    }
                )
                for (
                    opening,
                    received,
                    processed,
                    cv_review,
                    shortlisted,
                    app_review,
                    rejected,
                ) in rows
            }
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
