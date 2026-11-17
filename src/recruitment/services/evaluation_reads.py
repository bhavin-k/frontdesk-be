"""Tenant-scoped read projections for current evaluations and legacy match history."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from recruitment.adapters.db.matching import MatchingEvaluation, RoleAssessment
from recruitment.adapters.db.models import Application, MatchRun, Opening, ReviewDecision
from recruitment.services.common import missing


def legacy_match(run: MatchRun) -> dict[str, Any]:
    return {
        "match_run_id": run.id,
        "score": run.score,
        "recommendation": run.recommendation,
        "reason": run.reason,
    }


def role_match(session: Session, role: RoleAssessment) -> dict[str, Any] | None:
    if role.match_result is not None:
        return role.match_result
    run = session.get(MatchRun, role.match_run_id) if role.match_run_id else None
    return legacy_match(run) if run else None


def assessment_read(
    session: Session, role: RoleAssessment, evaluation: MatchingEvaluation
) -> dict[str, Any]:
    opening = session.get(Opening, role.opening_id)
    source = next(
        (item for item in evaluation.role_snapshot if item["opening_id"] == str(role.opening_id)),
        {},
    )
    return {
        "id": role.id,
        "opening_id": role.opening_id,
        "opening_title": source.get("jd", {}).get("title")
        or source.get("title")
        or (opening.title if opening else ""),
        "opening_status": opening.status if opening else "closed",
        "state": role.state,
        "criteria_version_id": role.criteria_version_id,
        "match_result": role.match_result,
        "score": role.score,
        "score_provisional": role.provisional,
        "confidence": role.confidence,
        "recommendation": role.recommendation,
        "reason": role.reason,
        "match_run_id": role.match_run_id,
        "failure_code": role.failure_code,
    }


def read_results(session: Session, app: Application) -> dict[str, Any]:
    evaluation = (
        session.get(MatchingEvaluation, app.latest_evaluation_id)
        if app.latest_evaluation_id
        else None
    )
    if evaluation and evaluation.resume_id == app.current_resume_id:
        roles = list(
            session.scalars(
                select(RoleAssessment)
                .where(RoleAssessment.evaluation_id == evaluation.id)
                .order_by(RoleAssessment.opening_id)
            )
        )
        best = next(
            (role for role in roles if role.id == evaluation.recommended_assessment_id), None
        )
        return {
            "status": app.status,
            "result": role_match(session, best)
            if best and evaluation.state == "completed"
            else None,
            "evaluation": {
                "id": evaluation.id,
                "resume_id": evaluation.resume_id,
                "state": evaluation.state,
                "recommendation": evaluation.recommendation,
                "reason": evaluation.reason,
                "confidence": evaluation.confidence,
                "recommended_assessment_id": evaluation.recommended_assessment_id,
                "processed_at": evaluation.processed_at,
                "processing_config": evaluation.config,
                "roles": [assessment_read(session, role, evaluation) for role in roles],
            },
        }
    run = session.get(MatchRun, app.latest_match_id) if app.latest_match_id else None
    return {
        "status": app.status,
        "result": legacy_match(run) if run and run.resume_id == app.current_resume_id else None,
        "evaluation": None,
    }


def read_assessment(session: Session, app: Application, identifier: Any) -> dict[str, Any]:
    role = session.get(RoleAssessment, identifier)
    evaluation = session.get(MatchingEvaluation, role.evaluation_id) if role else None
    if (
        not role
        or not evaluation
        or evaluation.application_id != app.id
        or evaluation.resume_id != app.current_resume_id
        or evaluation.id != app.latest_evaluation_id
    ):
        raise missing()
    run = session.get(MatchRun, role.match_run_id) if role.match_run_id else None
    if run and (run.application_id != app.id or run.resume_id != evaluation.resume_id):
        raise missing()
    return {
        "assessment": assessment_read(session, role, evaluation),
        "result": role_match(session, role),
    }


def saved_decision(session: Session, app: Application) -> dict[str, Any] | None:
    record = session.scalar(
        select(ReviewDecision)
        .where(ReviewDecision.application_id == app.id)
        .order_by(ReviewDecision.created_at.desc())
        .limit(1)
    )
    if not record:
        return None
    evaluation = (
        session.get(MatchingEvaluation, record.evaluation_id) if record.evaluation_id else None
    )
    run = session.get(MatchRun, record.match_id) if record.match_id else None
    if (evaluation and evaluation.resume_id == app.current_resume_id) or (
        run and run.resume_id == app.current_resume_id
    ):
        return {
            "decision": record.decision,
            "saved_at": record.created_at,
            "recruiter_id": record.recruiter_id,
        }
    return None
