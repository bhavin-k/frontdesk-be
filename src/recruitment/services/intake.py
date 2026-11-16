"""Shared atomic CV storage for HTTP uploads and mailbox attachment jobs."""

import hashlib
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import (
    Application,
    CVUpload,
    Opening,
    ResumeFile,
    RetentionSetting,
)
from recruitment.adapters.db.phase2 import IdentityReview
from recruitment.adapters.file_storage import PostgresFileStorage
from recruitment.core.config import ScoringStrategy
from recruitment.core.errors import AppError
from recruitment.core.time import utcnow
from recruitment.domain.retention import deadline
from recruitment.ports.providers import FileStorage
from recruitment.services.common import advisory_lock, emit, missing
from recruitment.services.openings import intake_allowed


def store_cv(
    session: Session,
    tenant_id: UUID,
    opening_id: UUID,
    content: bytes,
    mime_type: str,
    source_received_at: datetime | None = None,
    storage_factory: Callable[[Session, UUID], FileStorage] = PostgresFileStorage,
    *,
    scoring_strategy: ScoringStrategy,
) -> tuple[CVUpload, bool]:
    opening = session.scalar(select(Opening).where(Opening.id == opening_id).with_for_update())
    if not opening:
        raise missing()
    if not intake_allowed(session, opening, source_received_at):
        raise AppError("OPENING_NOT_ACTIVE", "This opening is not accepting CVs.", 409)
    digest = hashlib.sha256(content).hexdigest()
    advisory_lock(session, f"file:{tenant_id}:{digest}")
    existing = session.scalar(
        select(CVUpload)
        .join(ResumeFile, CVUpload.file_id == ResumeFile.id)
        .where(ResumeFile.sha256 == digest)
    )
    if existing:
        if existing.opening_id != opening_id:
            cases = session.scalars(
                select(IdentityReview).where(
                    IdentityReview.upload_id == existing.id,
                    IdentityReview.kind == "cross_role_submission",
                )
            )
            if not any(
                case.proposed_contacts.get("opening_id") == str(opening_id) for case in cases
            ):
                app = (
                    session.get(Application, existing.application_id)
                    if existing.application_id
                    else None
                )
                existing_opening = session.get(Opening, existing.opening_id)
                assert existing_opening is not None
                session.add(
                    IdentityReview(
                        tenant_id=tenant_id,
                        upload_id=existing.id,
                        target_opening_id=opening_id,
                        candidate_id=app.candidate_id if app else None,
                        kind="cross_role_submission",
                        proposed_contacts={
                            "opening_id": str(opening_id),
                            "opening_title": opening.title,
                            "existing_opening_id": str(existing.opening_id),
                            "existing_opening_title": existing_opening.title,
                        },
                        candidate_options=[str(app.candidate_id)] if app else [],
                    )
                )
        return existing, False
    settings = session.scalar(select(RetentionSetting))
    assert settings is not None
    file_id = UUID(storage_factory(session, tenant_id).store(content, mime_type))
    record = CVUpload(
        id=uuid4(),
        tenant_id=tenant_id,
        opening_id=opening_id,
        file_id=file_id,
        source_received_at=source_received_at or utcnow(),
        retention_expires_at=deadline(
            utcnow(), None, other_days=settings.other_candidate_retention_days
        ),
    )
    session.add(record)
    session.flush()
    if opening.criteria_status == "ready":
        emit(
            session,
            tenant_id,
            "cv.uploaded",
            record.id,
            f"cv.uploaded:{record.id}",
            scoring_strategy=scoring_strategy,
        )
    elif opening.criteria_status == "failed":
        record.status, record.review_reason = "needs_review", "JOB_REQUIREMENTS_FAILED"
    return record, True
