"""Evaluate one saved CV profile against the active openings in its tenant."""

from copy import copy
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from recruitment.adapters.ai import JDMatcher
from recruitment.adapters.db.matching import MatchingEvaluation, RoleAssessment
from recruitment.adapters.db.models import (
    Application,
    CVUpload,
    MatchRun,
    RetentionSetting,
    ReviewDecision,
)
from recruitment.adapters.providers import MATCHER_PROMPT, Providers
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.contracts import CandidateProfile, JobDescription
from recruitment.domain.matching import checked_match, masked_profile
from recruitment.domain.retention import deadline
from recruitment.domain.scoring_contracts import POLICY_VERSION
from recruitment.ports.queue import Lease, WorkerQueue
from recruitment.services.common import emit
from recruitment.services.scoring_rubrics import prepare_rubric


def has_decision(session: Session, app: Application, resume_id: UUID) -> bool:
    return (
        session.scalar(
            select(ReviewDecision.id)
            .where(
                ReviewDecision.application_id == app.id,
                ReviewDecision.evaluation_id.in_(
                    select(MatchingEvaluation.id).where(MatchingEvaluation.resume_id == resume_id)
                )
                | ReviewDecision.match_id.in_(
                    select(MatchRun.id).where(MatchRun.resume_id == resume_id)
                ),
            )
            .limit(1)
        )
        is not None
    )


class MatchingWorkflow:
    def __init__(self, queue: WorkerQueue, providers: Providers) -> None:
        self.queue, self.providers = queue, providers

    def process(self, lease: Lease, resume_id: UUID, profile: CandidateProfile) -> None:
        work = self._begin(lease, resume_id)
        if work is None:
            return
        evaluation_id, jobs, prompt = work
        matcher = self.providers.matcher
        if isinstance(matcher, JDMatcher) and matcher.prompt != prompt:
            matcher = copy(matcher)
            matcher.prompt = prompt
        masked = masked_profile(profile)
        exhausted: ProviderError | None = None
        for role_id, job in jobs:
            try:
                if prompt == MATCHER_PROMPT and job.rubric is None:
                    with self.queue.rubric_scope(lease) as session:
                        job.rubric = prepare_rubric(
                            session, self.providers.rubrics, job, lease.tenant_id
                        )
                    with self.queue.owned(lease) as session:
                        evaluation = session.get(MatchingEvaluation, evaluation_id)
                        assert evaluation is not None
                        role = session.get(RoleAssessment, role_id)
                        assert role is not None
                        evaluation.role_snapshot = [
                            {**row, "jd": job.model_dump()}
                            if row["opening_id"] == str(role.opening_id)
                            else row
                            for row in evaluation.role_snapshot
                        ]
                match = checked_match(job, profile, matcher.match(job, masked))
            except ProviderError as exc:
                if exc.retryable:
                    if lease.attempts < self.queue.settings.max_job_attempts:
                        raise
                    exhausted = exc
                with self.queue.owned(lease) as session:
                    role = session.get(RoleAssessment, role_id)
                    assert role is not None
                    role.state, role.failure_code = "failed", exc.code
                    role.reason = "Matching could not be completed for this opening."
                continue
            with self.queue.owned(lease) as session:
                role = session.get(RoleAssessment, role_id)
                assert role is not None
                role.state = "completed"
                role.match_result = match.model_dump()
                role.score = Decimal(str(match.score))
                role.reason = match.reason
                role.recommendation = (
                    "recommended_shortlist" if match.fit else "recommended_rejection"
                )
        self._publish(lease, evaluation_id, resume_id)
        if exhausted is not None:
            raise exhausted

    def _begin(
        self, lease: Lease, resume_id: UUID
    ) -> tuple[UUID, list[tuple[UUID, JobDescription]], str] | None:
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None and upload.application_id
            app = session.get(Application, upload.application_id, with_for_update=True)
            assert app is not None
            if app.rejected_at or has_decision(session, app, resume_id):
                upload.status = "completed"
                return None

            roles = upload.matching_openings
            if any(row.get("scoring_policy", POLICY_VERSION) != POLICY_VERSION for row in roles):
                raise ProviderError("PROCESSING_CONFIG_CHANGED")
            config = {
                "pipeline": "cv-json-v1",
                "matcher": self.providers.matcher.model,
                "provider": self.providers.name,
                "endpoint": self.queue.settings.llm_base_url,
                "format": self.queue.settings.llm_api(),
                "prompt": MATCHER_PROMPT,
                "scoring_policy": POLICY_VERSION,
            }
            signature = "cv-json-v1"
            evaluation = session.scalar(
                select(MatchingEvaluation).where(
                    MatchingEvaluation.resume_id == resume_id,
                    MatchingEvaluation.signature == signature,
                )
            )
            if evaluation is None:
                legacy_by_opening: dict[UUID, RoleAssessment] = {}
                for old in session.scalars(
                    select(RoleAssessment)
                    .join(MatchingEvaluation, MatchingEvaluation.id == RoleAssessment.evaluation_id)
                    .where(
                        MatchingEvaluation.resume_id == resume_id,
                        RoleAssessment.state == "completed",
                    )
                    .order_by(RoleAssessment.created_at.desc(), RoleAssessment.id)
                ):
                    legacy_by_opening.setdefault(old.opening_id, old)
                evaluation = MatchingEvaluation(
                    id=uuid4(),
                    tenant_id=lease.tenant_id,
                    application_id=app.id,
                    resume_id=resume_id,
                    signature=signature,
                    role_snapshot=roles,
                    config=config,
                )
                session.add(evaluation)
                session.flush()
                for row in roles:
                    opening = UUID(row["opening_id"])
                    role = RoleAssessment(
                        tenant_id=lease.tenant_id,
                        application_id=app.id,
                        evaluation_id=evaluation.id,
                        opening_id=opening,
                    )
                    previous = legacy_by_opening.get(opening)
                    if previous is not None:
                        role.state, role.score = "completed", previous.score
                        role.recommendation = previous.recommendation
                        role.reason = previous.reason
                        role.match_run_id = previous.match_run_id
                        role.provisional = previous.provisional
                        role.confidence = previous.confidence
                    session.add(role)
                session.flush()
            elif evaluation.state == "completed":
                return None
            if app.current_resume_id == resume_id and app.latest_evaluation_id != evaluation.id:
                app.latest_evaluation_id, app.latest_match_id = evaluation.id, None
                app.status, app.lock_version = "processing", app.lock_version + 1
            prompt = evaluation.config.get("prompt")
            comparable = {
                **evaluation.config,
                "prompt": config["prompt"],
                "scoring_policy": POLICY_VERSION,
            }
            if (
                prompt not in {"jd-matcher-v1", "jd-matcher-v2", MATCHER_PROMPT}
                or evaluation.config.get("scoring_policy", POLICY_VERSION) != POLICY_VERSION
                or comparable != config
            ):
                raise ProviderError("PROCESSING_CONFIG_CHANGED")
            evaluation.state = "processing"
            upload.status = "matching"
            descriptions = {row["opening_id"]: row["jd"] for row in evaluation.role_snapshot}
            pending = session.scalars(
                select(RoleAssessment)
                .where(
                    RoleAssessment.evaluation_id == evaluation.id, RoleAssessment.state == "pending"
                )
                .order_by(RoleAssessment.opening_id)
            )
            return (
                evaluation.id,
                [
                    (role.id, JobDescription.model_validate(descriptions[str(role.opening_id)]))
                    for role in pending
                ],
                prompt,
            )

    def _publish(self, lease: Lease, evaluation_id: UUID, resume_id: UUID) -> None:
        with self.queue.owned(lease) as session:
            evaluation = session.get(MatchingEvaluation, evaluation_id)
            upload = session.get(CVUpload, lease.aggregate_id)
            assert evaluation is not None and upload is not None
            app = session.get(Application, evaluation.application_id, with_for_update=True)
            policy = session.scalar(select(RetentionSetting))
            assert app is not None and policy is not None
            if (
                app.rejected_at
                or deadline(
                    app.last_activity_at,
                    app.rejected_at,
                    policy.rejected_retention_days,
                    policy.other_candidate_retention_days,
                )
                <= utcnow()
            ):
                raise ProviderError(
                    "RETENTION_EXPIRED" if not app.rejected_at else "APPLICATION_CLOSED"
                )
            roles = list(
                session.scalars(
                    select(RoleAssessment)
                    .where(RoleAssessment.evaluation_id == evaluation.id)
                    .order_by(RoleAssessment.score.desc().nulls_last(), RoleAssessment.opening_id)
                )
            )
            scored = [role for role in roles if role.score is not None]
            fits = [role for role in scored if role.recommendation == "recommended_shortlist"]
            failed = next((role for role in roles if role.state == "failed"), None)
            ranked = fits or scored
            best = ranked[0] if ranked else None
            if failed or not roles or any(role.recommendation == "needs_review" for role in roles):
                recommendation = "needs_review"
                reason = "An opening could not be matched." if roles else "No active openings."
            elif len(fits) > 1 and (fits[0].score or 0) - (fits[1].score or 0) <= 5:
                recommendation, reason = (
                    "needs_review",
                    "Two fitting openings scored within five points.",
                )
            elif upload.review_reason in {"RESUME_UPDATED", "REAPPLICATION"}:
                recommendation = "needs_review"
                reason = (
                    "Review this new CV alongside the candidate's previous application history."
                )
            elif fits:
                recommendation, reason = "recommended_shortlist", fits[0].reason
            else:
                recommendation = "recommended_rejection"
                reason = best.reason if best else "No active opening fits the CV."
            evaluation.state, evaluation.processed_at = "completed", utcnow()
            evaluation.recommendation, evaluation.reason = recommendation, reason
            evaluation.recommended_assessment_id = best.id if best else None
            if app.current_resume_id == resume_id and app.latest_evaluation_id == evaluation.id:
                app.status, app.lock_version = "needs_review", app.lock_version + 1
            upload.status = "needs_review" if failed else "completed"
            if app.current_resume_id != resume_id:
                upload.review_reason = "SUPERSEDED_CV"
            elif upload.review_reason not in {"RESUME_UPDATED", "REAPPLICATION"}:
                upload.review_reason = failed.failure_code if failed else None
            emit(
                session,
                lease.tenant_id,
                "match.completed",
                app.id,
                f"match.completed:{evaluation.id}",
            )
            emit(
                session,
                lease.tenant_id,
                "review.required",
                app.id,
                f"review.required:{evaluation.id}",
            )
