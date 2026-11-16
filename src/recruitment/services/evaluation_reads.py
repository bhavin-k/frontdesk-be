"""Tenant-scoped read projections for current evaluations and legacy match history."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from recruitment.adapters.db.matching import MatchingEvaluation, RoleAssessment
from recruitment.adapters.db.models import (
    Application,
    CriteriaVersion,
    CriterionResult,
    JobCriterion,
    MatchRun,
    Opening,
    ReviewDecision,
)
from recruitment.services.common import missing


def match_read(
    session: Session, run: MatchRun, role: RoleAssessment | None = None
) -> dict[str, Any]:
    version = session.get(CriteriaVersion, run.criteria_version_id)
    assert version is not None
    opening = session.get(Opening, version.opening_id)
    criteria = {
        item.id: item
        for item in session.scalars(
            select(JobCriterion).where(JobCriterion.version_id == version.id)
        )
    }
    grades = list(
        session.scalars(select(CriterionResult).where(CriterionResult.match_id == run.id))
    )
    grades.sort(key=lambda item: criteria[item.criterion_id].position)
    return {
        "application_id": run.application_id,
        "match_run_id": run.id,
        "criteria_version_id": version.id,
        "opening_id": version.opening_id,
        "opening_title": opening.title if opening else None,
        "score": run.score,
        "score_provisional": role.provisional if role else False,
        "confidence": role.confidence if role else None,
        "shortlist_threshold": version.shortlist_threshold,
        "recommendation": run.recommendation,
        "reason": run.reason,
        "model": run.model,
        "provider": run.provider,
        "prompt_version": run.prompt_version,
        "scoring_strategy": run.scoring_strategy,
        "embedding_model": run.embedding_model,
        "embedding_dimensions": run.embedding_dimensions,
        "embedding_fingerprint": run.embedding_fingerprint,
        "processing_config": run.processing_config,
        "missing_required_criteria": run.missing_required_criteria,
        "processed_at": run.processed_at,
        "criteria": [
            {
                "criterion_id": grade.criterion_id,
                "criterion": criteria[grade.criterion_id].text,
                "required": criteria[grade.criterion_id].required,
                "strict": criteria[grade.criterion_id].strict,
                "weight": criteria[grade.criterion_id].weight,
                "status": grade.status,
                "confidence": grade.confidence,
                "reason": grade.reason,
                "evidence": grade.evidence,
            }
            for grade in grades
        ],
    }


def assessment_read(session: Session, role: RoleAssessment) -> dict[str, Any]:
    opening = session.get(Opening, role.opening_id)
    return {
        "id": role.id,
        "opening_id": role.opening_id,
        "opening_title": opening.title if opening else "",
        "opening_status": opening.status if opening else "closed",
        "state": role.state,
        "criteria_version_id": role.criteria_version_id,
        "eligibility": role.eligibility,
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
        run = session.get(MatchRun, best.match_run_id) if best and best.match_run_id else None
        return {
            "status": app.status,
            "result": match_read(session, run, best)
            if run and evaluation.state == "completed"
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
                "roles": [assessment_read(session, role) for role in roles],
            },
        }
    run = session.get(MatchRun, app.latest_match_id) if app.latest_match_id else None
    return {
        "status": app.status,
        "result": match_read(session, run)
        if run and run.resume_id == app.current_resume_id
        else None,
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
        "assessment": assessment_read(session, role),
        "result": match_read(session, run, role) if run else None,
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
