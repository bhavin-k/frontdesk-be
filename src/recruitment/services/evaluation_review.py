"""Recruiter approval, isolated from the model recommendation and its reason."""

from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, update

from recruitment.adapters.db.matching import MatchingEvaluation, RoleAssessment
from recruitment.adapters.db.models import Application, CVUpload, RetentionSetting, ReviewDecision
from recruitment.core.errors import AppError
from recruitment.core.time import utcnow
from recruitment.domain.retention import deadline
from recruitment.services.auth import Principal
from recruitment.services.common import emit, existing_request, missing, remember_request
from recruitment.services.matching import active_roles
from recruitment.services.tenancy import TenancyService


def decide_evaluation(
    tenants: TenancyService,
    principal: Principal,
    tenant_id: UUID,
    app_id: UUID,
    evaluation_id: UUID,
    expected_version: int,
    role_assessment_id: UUID | None,
    decision: str,
    confirmed: bool,
    reason: str | None,
    key: str,
) -> tuple[dict[str, Any], bool]:
    if decision == "reject" and not confirmed:
        raise AppError("CONFIRMATION_REQUIRED", "Confirm the rejection before saving it.")
    payload = {
        "app": app_id,
        "evaluation": evaluation_id,
        "version": expected_version,
        "assessment": role_assessment_id,
        "decision": decision,
        "confirm": confirmed,
        "reason": reason,
    }
    with tenants.scope(principal, tenant_id) as session:
        previous = existing_request(session, tenant_id, "decision", key, payload)
        if previous:
            record = session.get(ReviewDecision, previous)
            app = session.get(Application, app_id)
            if not record or not app:
                raise missing()
            return {
                "id": record.id,
                "decision": record.decision,
                "status": app.status,
                "retention_expires_at": app.retention_expires_at,
            }, False
        app = session.scalar(select(Application).where(Application.id == app_id).with_for_update())
        if not app:
            raise missing()
        evaluation = session.get(MatchingEvaluation, evaluation_id)
        if not evaluation or evaluation.application_id != app.id:
            raise missing()
        if (
            app.latest_evaluation_id != evaluation.id
            or app.current_resume_id != evaluation.resume_id
            or app.lock_version != expected_version
            or app.rejected_at
            or evaluation.state != "completed"
            or active_roles(session) != evaluation.role_snapshot
        ):
            raise AppError("STALE_DECISION", "Refresh the current evaluation before deciding.", 409)
        if app.retention_expires_at <= utcnow():
            raise AppError("STALE_DECISION", "This application has expired.", 409)
        role = session.get(RoleAssessment, role_assessment_id) if role_assessment_id else None
        if role_assessment_id and (not role or role.evaluation_id != evaluation.id):
            raise missing()
        if decision == "shortlist" and role is None:
            raise AppError("ROLE_REQUIRED", "Select one active role before saving.", 422)
        record = ReviewDecision(
            id=uuid4(),
            tenant_id=tenant_id,
            application_id=app.id,
            evaluation_id=evaluation.id,
            role_assessment_id=role.id if role else None,
            match_id=role.match_run_id if role else None,
            recruiter_id=principal.user_id,
            decision=decision,
            reason=reason,
        )
        session.add(record)
        session.flush()
        if decision == "shortlist" and role:
            app.opening_id, app.latest_match_id = role.opening_id, role.match_run_id
        now = utcnow()
        app.status, app.rejected_at = (
            ("rejected", now) if decision == "reject" else ("shortlisted", None)
        )
        app.last_activity_at, app.lock_version = now, app.lock_version + 1
        policy = session.scalar(select(RetentionSetting))
        assert policy is not None
        app.retention_expires_at = deadline(
            now,
            app.rejected_at,
            policy.rejected_retention_days,
            policy.other_candidate_retention_days,
        )
        session.execute(
            update(CVUpload)
            .where(CVUpload.application_id == app.id)
            .values(retention_expires_at=app.retention_expires_at)
        )
        emit(session, tenant_id, "review.recorded", app.id, f"review.recorded:{record.id}")
        if decision == "shortlist":
            emit(session, tenant_id, "shortlist.ready", app.id, f"shortlist.ready:{record.id}")
        remember_request(session, tenant_id, "decision", key, payload, record.id)
        return {
            "id": record.id,
            "decision": decision,
            "status": app.status,
            "retention_expires_at": app.retention_expires_at,
        }, True
