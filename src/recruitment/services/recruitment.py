import hashlib
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import (
    Application,
    Candidate,
    CVUpload,
    Opening,
    Resume,
    RetentionSetting,
    ReviewDecision,
)
from recruitment.adapters.db.phase2 import IdentityReview
from recruitment.adapters.documents import validate_file
from recruitment.adapters.providers import Providers
from recruitment.core.config import Settings
from recruitment.core.errors import AppError, ProviderError
from recruitment.core.time import ist_display, utcnow
from recruitment.domain.retention import deadline
from recruitment.ports.providers import FileStorage
from recruitment.services.auth import Principal
from recruitment.services.common import (
    advisory_lock,
    emit,
    existing_request,
    missing,
    remember_request,
)
from recruitment.services.evaluation_reads import read_assessment, read_results, saved_decision
from recruitment.services.intake import store_cv
from recruitment.services.openings import OpeningService
from recruitment.services.screening_records import start_screening
from recruitment.services.tenancy import TenancyService


class RecruitmentService:
    def __init__(
        self,
        tenants: TenancyService,
        providers: Providers,
        storage_factory: Callable[[Session, UUID], FileStorage],
        settings: Settings,
    ) -> None:
        self.tenants, self.providers = tenants, providers
        self.storage_factory = storage_factory
        self.settings = settings

    @staticmethod
    def _opening(session: Session, opening_id: UUID) -> dict[str, Any]:
        return OpeningService.read_session(session, opening_id)

    def uploads(
        self,
        principal: Principal,
        tenant_id: UUID,
        limit: int,
        offset: int,
        opening_id: UUID | None = None,
    ) -> dict[str, Any]:
        with self.tenants.scope(principal, tenant_id) as session:
            query = select(CVUpload.id)
            if opening_id:
                self._opening(session, opening_id)
                query = query.where(CVUpload.opening_id == opening_id)
            ids = session.scalars(
                query.order_by(CVUpload.created_at.desc(), CVUpload.id).offset(offset).limit(limit)
            ).all()
            return {
                "items": [self._upload(session, item) for item in ids],
                "limit": limit,
                "offset": offset,
            }

    def upload(
        self,
        principal: Principal,
        tenant_id: UUID,
        opening_id: UUID,
        content: bytes,
        mime_type: str,
        key: str,
    ) -> tuple[dict[str, Any], bool]:
        received_at = utcnow()
        validate_file(content, mime_type)
        # Authorize before calling even a local scanner.
        with self.tenants.scope(principal, tenant_id) as session:
            opening = session.get(Opening, opening_id)
            if not opening:
                raise missing()
            if opening.status != "active":
                raise AppError("OPENING_NOT_ACTIVE", "This opening is not accepting CVs.", 409)
        try:
            self.providers.scanner.scan(content)
        except ProviderError as exc:
            raise AppError(
                exc.code, "The security scan could not complete. Try again.", 503
            ) from exc
        digest = hashlib.sha256(content).hexdigest()
        payload = {"opening": str(opening_id), "sha256": digest, "mime_type": mime_type}
        with self.tenants.scope(principal, tenant_id) as session:
            previous = existing_request(session, tenant_id, "upload", key, payload)
            if previous:
                return self._upload(session, previous), False
            record, created = store_cv(
                session,
                tenant_id,
                opening_id,
                content,
                mime_type,
                source_received_at=received_at,
                storage_factory=self.storage_factory,
            )
            remember_request(session, tenant_id, "upload", key, payload, record.id)
            return self._upload(session, record.id), created

    @staticmethod
    def _upload(session: Session, upload_id: UUID) -> dict[str, Any]:
        record = session.get(CVUpload, upload_id)
        if not record:
            raise missing()
        return {
            "upload_id": record.id,
            "status": record.status,
            "application_id": record.application_id,
            "review_reason": record.review_reason,
            "retention_expires_at": record.retention_expires_at,
        }

    def upload_status(
        self, principal: Principal, tenant_id: UUID, upload_id: UUID
    ) -> dict[str, Any]:
        with self.tenants.scope(principal, tenant_id) as session:
            return self._upload(session, upload_id)

    def application(self, principal: Principal, tenant_id: UUID, app_id: UUID) -> dict[str, Any]:
        with self.tenants.scope(principal, tenant_id) as session:
            app = session.get(Application, app_id)
            if not app:
                raise missing()
            candidate = session.get(Candidate, app.candidate_id)
            assert candidate is not None
            return {
                "id": app.id,
                "candidate_id": app.candidate_id,
                "opening_id": app.opening_id,
                "profile": candidate.profile,
                "status": app.status,
                "latest_match_id": app.latest_match_id,
                "latest_evaluation_id": app.latest_evaluation_id,
                "version": app.lock_version,
                "recruiter_decision": saved_decision(session, app),
                "retention_expires_at": app.retention_expires_at,
                "retention_display_ist": ist_display(app.retention_expires_at),
            }

    def results(self, principal: Principal, tenant_id: UUID, app_id: UUID) -> dict[str, Any]:
        with self.tenants.scope(principal, tenant_id) as session:
            app = session.get(Application, app_id)
            if not app:
                raise missing()
            return read_results(session, app)

    def assessment(
        self, principal: Principal, tenant_id: UUID, app_id: UUID, assessment_id: UUID
    ) -> dict[str, Any]:
        with self.tenants.scope(principal, tenant_id) as session:
            app = session.get(Application, app_id)
            if not app:
                raise missing()
            return read_assessment(session, app, assessment_id)

    def review_queue(
        self, principal: Principal, tenant_id: UUID, limit: int, offset: int
    ) -> dict[str, Any]:
        with self.tenants.scope(principal, tenant_id) as session:
            uploads = session.scalars(
                select(CVUpload)
                .where(CVUpload.status == "needs_review")
                .order_by(CVUpload.created_at, CVUpload.id)
                .offset(offset)
                .limit(limit)
            ).all()
            apps = session.scalars(
                select(Application)
                .where(Application.status == "needs_review")
                .order_by(Application.created_at, Application.id)
                .offset(offset)
                .limit(limit)
            ).all()
            return {
                "uploads": [self._upload(session, item.id) for item in uploads],
                "identity_review_ids": list(
                    session.scalars(
                        select(IdentityReview.id)
                        .where(IdentityReview.status == "open")
                        .order_by(IdentityReview.created_at, IdentityReview.id)
                        .offset(offset)
                        .limit(limit)
                    )
                ),
                "applications": [
                    {
                        "id": item.id,
                        "status": item.status,
                        "latest_match_id": item.latest_match_id,
                        "candidate_id": item.candidate_id,
                        "name": (
                            session.scalar(
                                select(Candidate.profile).where(Candidate.id == item.candidate_id)
                            )
                            or {}
                        ).get("name"),
                        "upload_id": session.scalar(
                            select(Resume.upload_id).where(Resume.id == item.current_resume_id)
                        ),
                    }
                    for item in apps
                ],
                "limit": limit,
                "offset": offset,
            }

    def decide(
        self,
        principal: Principal,
        tenant_id: UUID,
        app_id: UUID,
        match_id: UUID,
        decision: str,
        confirm_rejection: bool,
        reason: str | None,
        key: str,
    ) -> tuple[dict[str, Any], bool]:
        if decision == "reject" and not confirm_rejection:
            raise AppError("CONFIRMATION_REQUIRED", "Confirm the rejection before saving it.")
        payload = {
            "app": app_id,
            "match": match_id,
            "decision": decision,
            "reason": reason,
            "confirm": confirm_rejection,
        }
        with self.tenants.scope(principal, tenant_id) as session:
            previous = existing_request(session, tenant_id, "decision", key, payload)
            if previous:
                row = session.get(ReviewDecision, previous)
                if not row:
                    raise missing()
                original_app = session.get(Application, row.application_id)
                if not original_app:
                    raise missing()
                return {
                    "id": row.id,
                    "decision": row.decision,
                    "status": original_app.status,
                    "retention_expires_at": original_app.retention_expires_at,
                }, False
            advisory_lock(session, f"tenant:{tenant_id}")
            app = session.scalar(
                select(Application).where(Application.id == app_id).with_for_update()
            )
            if not app:
                raise missing()
            from recruitment.adapters.db.models import MatchRun

            current_result = session.get(MatchRun, match_id)
            if (
                app.latest_match_id != match_id
                or app.rejected_at
                or not current_result
                or current_result.resume_id != app.current_resume_id
            ):
                raise AppError("STALE_DECISION", "Refresh the current result before deciding.", 409)
            from recruitment.adapters.db.matching import MatchingEvaluation, RoleAssessment

            evaluation = (
                session.get(MatchingEvaluation, app.latest_evaluation_id)
                if app.latest_evaluation_id
                else None
            )
            assessment = None
            if evaluation:
                assessment = session.scalar(
                    select(RoleAssessment).where(
                        RoleAssessment.evaluation_id == evaluation.id,
                        RoleAssessment.match_run_id == match_id,
                    )
                )
                if evaluation.state != "completed" or not assessment:
                    raise AppError(
                        "STALE_DECISION", "Refresh the current evaluation before deciding.", 409
                    )
                if decision == "shortlist":
                    opening = session.get(Opening, assessment.opening_id)
                    if not opening or opening.status != "active":
                        raise AppError("OPENING_NOT_ACTIVE", "Select an active opening.", 409)
                    app.opening_id = assessment.opening_id
            record = ReviewDecision(
                id=uuid4(),
                tenant_id=tenant_id,
                application_id=app_id,
                match_id=match_id,
                evaluation_id=evaluation.id if evaluation else None,
                role_assessment_id=assessment.id if assessment else None,
                recruiter_id=principal.user_id,
                decision=decision,
                reason=reason,
            )
            session.add(record)
            session.flush()
            now = utcnow()
            app.status = "rejected" if decision == "reject" else "shortlisted"
            app.rejected_at = now if decision == "reject" else None
            app.last_activity_at = now
            settings = session.scalar(select(RetentionSetting))
            assert settings is not None
            app.retention_expires_at = deadline(
                now,
                app.rejected_at,
                settings.rejected_retention_days,
                settings.other_candidate_retention_days,
            )
            app.lock_version += 1
            session.execute(
                update(CVUpload)
                .where(CVUpload.application_id == app.id)
                .values(retention_expires_at=app.retention_expires_at)
            )
            emit(session, tenant_id, "review.recorded", app.id)
            if decision == "shortlist":
                emit(session, tenant_id, "shortlist.ready", app.id)
                start_screening(session, self.settings, app, principal.user_id)
            remember_request(session, tenant_id, "decision", key, payload, record.id)
            return {
                "id": record.id,
                "decision": decision,
                "status": app.status,
                "retention_expires_at": app.retention_expires_at,
            }, True
