"""Experience-first evaluation across active roles, with durable grading checkpoints."""

import hashlib
import json
import re
from dataclasses import replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, aliased

from recruitment.adapters.db.matching import MatchingEvaluation, RoleAssessment
from recruitment.adapters.db.models import (
    Application,
    CriteriaVersion,
    CriterionResult,
    CVUpload,
    JobCriterion,
    MatchRun,
    Opening,
    ResumeChunk,
    RetentionSetting,
    ReviewDecision,
)
from recruitment.adapters.db.openings import OpeningDocument
from recruitment.adapters.db.phase2 import EmbeddingBatch
from recruitment.adapters.providers import Providers
from recruitment.core.config import ScoringStrategy
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.contracts import (
    CandidateProfile,
    Criterion,
    EvidenceGrade,
    EvidenceInput,
    Extraction,
    Grade,
)
from recruitment.domain.experience import (
    POLICY_VERSION,
    ExperienceFacts,
    ExperienceMinimum,
    ExperienceSource,
    eligibility,
    validate_facts,
)
from recruitment.domain.full_text import (
    FULL_TEXT_PROMPT_VERSION,
    FullTextDocument,
    FullTextGrade,
    FullTextResponse,
    FullTextRubric,
    QuoteSelection,
    RubricCriterion,
    verify_full_text,
)
from recruitment.domain.matching import calculate_verified_match, scoring_text, validate_evidence
from recruitment.domain.rag import RankedChunk, evidence_selection, keyword_rank
from recruitment.domain.retention import deadline
from recruitment.ports.queue import Lease, WorkerQueue
from recruitment.services.common import advisory_lock, emit

if TYPE_CHECKING:
    from recruitment.services.processing import CVProcessor

FULL_TEXT_POLICY_VERSION = f"{POLICY_VERSION}:{FULL_TEXT_PROMPT_VERSION}"
EXPERIENCE_SOURCE_MARKER = re.compile(
    r"\b(experienc\w*|years?|yrs?|months?|minimum|prefer\w*|optional|required|"
    r"qualification\w*|intern\w*|equivalent|exception\w*|waiv\w*|nice to have|at least)\b",
    re.IGNORECASE,
)


def minimum_experience_criterion(text: str) -> bool:
    if re.search(r"\b(total|relevant) experience years:", text, re.I):
        return True
    return bool(
        re.search(r"\bexperience\b", text, re.I)
        and re.search(
            r"\b(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)\s*\+?\s*(years?|yrs?|months?)\b",
            text,
            re.I,
        )
    )


def active_roles(session: Session) -> list[dict[str, Any]]:
    roles = []
    for row in session.scalars(
        select(Opening).where(Opening.status == "active").order_by(Opening.id)
    ):
        version = session.scalar(
            select(CriteriaVersion)
            .where(CriteriaVersion.opening_id == row.id)
            .order_by(CriteriaVersion.version.desc())
            .limit(1)
        )
        roles.append(
            {
                "opening_id": str(row.id),
                "title": row.title,
                "source_revision": row.source_revision,
                "criteria_status": row.criteria_status,
                "version_id": str(version.id)
                if version and row.criteria_status == "ready"
                else None,
            }
        )
    return roles


def requeue_pending(
    session: Session, tenant_id: UUID, reason: str, strategy: ScoringStrategy
) -> None:
    """Re-evaluate undecided current CVs after the available role set changes."""
    from recruitment.adapters.db.models import Resume

    for app in session.scalars(select(Application).where(Application.rejected_at.is_(None))):
        if not app.current_resume_id or app.retention_expires_at <= utcnow():
            continue
        decided = session.scalar(
            select(ReviewDecision.id)
            .where(
                ReviewDecision.application_id == app.id,
                ReviewDecision.match_id.in_(
                    select(MatchRun.id).where(MatchRun.resume_id == app.current_resume_id)
                )
                | ReviewDecision.evaluation_id.in_(
                    select(MatchingEvaluation.id).where(
                        MatchingEvaluation.resume_id == app.current_resume_id
                    )
                ),
            )
            .limit(1)
        )
        if decided:
            continue
        resume = session.get(Resume, app.current_resume_id)
        upload = session.get(CVUpload, resume.upload_id) if resume else None
        if upload:
            emit(
                session,
                tenant_id,
                "cv.uploaded",
                upload.id,
                f"cv.roles:{upload.id}:{reason}",
                scoring_strategy=strategy,
            )


def source_lines(
    prefix: str,
    text: str,
    *,
    opening_id: str | None = None,
    page: int | None = None,
    identities: list[str] | None = None,
) -> list[ExperienceSource]:
    safe = scoring_text(text, identities or [])
    result = []
    for match in re.finditer(r"[^\r\n]+", safe):
        value = match[0].strip()
        start = match.start() + len(match[0]) - len(match[0].lstrip())
        end = start + len(value)
        if value and text[start:end] == value:
            result.append(
                ExperienceSource(
                    id=f"{prefix}:{start}",
                    text=value,
                    opening_id=opening_id,
                    page=page,
                    char_start=start,
                    char_end=end,
                )
            )
    return result


def opening_experience_sources(
    prefix: str,
    text: str,
    opening_id: str,
    quotes: list[str] | None,
    *,
    primary: bool = False,
) -> list[ExperienceSource]:
    """Keep published evidence, possible minimums and neighboring context."""
    lines = source_lines(prefix, text, opening_id=opening_id)
    if quotes is None:
        # Older or unprepared roles have no verified evidence to use for selection.
        return lines
    ranges: list[tuple[int, int]] = []
    for quote in quotes:
        matches = list(re.finditer(re.escape(quote), text))
        if not matches:
            return lines
        ranges.extend(match.span() for match in matches)
    anchors = {
        index
        for index, line in enumerate(lines)
        if EXPERIENCE_SOURCE_MARKER.search(line.text)
        or any(start < line.char_end and line.char_start < end for start, end in ranges)
    }
    if primary and not anchors:
        return lines
    nearby = {index + distance for index in anchors for distance in (-1, 0, 1)}
    return [line for index, line in enumerate(lines) if index in nearby]


class MatchingWorkflow:
    def __init__(self, queue: WorkerQueue, providers: Providers, processor: "CVProcessor") -> None:
        self.queue, self.providers, self.processor = queue, providers, processor

    def process(self, lease: Lease) -> None:
        config = self.processor._snapshot(lease)
        strategy: ScoringStrategy = (
            "full_text" if config.get("scoring_strategy") == "full_text" else "rag"
        )
        if strategy == "full_text":
            with self.queue.serialize_cv_scoring(lease):
                self._process(lease, config, strategy)
        else:
            self._process(lease, config, strategy)

    def _process(self, lease: Lease, config: dict[str, Any], strategy: ScoringStrategy) -> None:
        extraction, profile = self.processor._extract(
            lease, cv_token_ceiling=config.get("cv_token_ceiling")
        )
        resume_id = self.processor._resolve(lease, profile, full_text=strategy == "full_text")
        if resume_id is None:
            return
        evaluation_id = self._begin(lease, resume_id, config)
        if evaluation_id is None:
            return
        facts, sources = self._experience(lease, evaluation_id, extraction, profile)
        versions = self._gate(lease, evaluation_id, facts, sources, profile)
        document = None
        if versions:
            if strategy == "full_text":
                document = FullTextDocument.from_pages(
                    extraction.pages,
                    [profile.name or "", profile.email or "", profile.mobile or ""],
                    cv_id=lease.aggregate_id,
                )
            else:
                self.processor._embed(lease, resume_id, extraction, profile, versions)
        retry_error = None
        with self.queue.owned(lease) as session:
            ids = list(
                session.scalars(
                    select(RoleAssessment.id)
                    .where(
                        RoleAssessment.evaluation_id == evaluation_id,
                        RoleAssessment.state == "pending",
                    )
                    .order_by(RoleAssessment.opening_id)
                )
            )
        for identifier in ids:
            try:
                self._grade(lease, evaluation_id, identifier, resume_id, profile, config, document)
            except ProviderError as error:
                with self.queue.owned(lease) as session:
                    failed_role = session.get(RoleAssessment, identifier)
                    assert failed_role is not None
                    if error.retryable and lease.attempts < self.queue.settings.max_job_attempts:
                        failed_role.state, failed_role.failure_code = "pending", error.code
                        retry_error = error
                    else:
                        self._failed(failed_role, error)
                if strategy == "full_text" and retry_error:
                    break
        if retry_error:
            raise retry_error
        self._publish(lease, evaluation_id, resume_id, profile, strategy)

    @staticmethod
    def _failed(role: RoleAssessment, error: ProviderError) -> None:
        role.state, role.failure_code = "failed", error.code
        role.recommendation, role.confidence, role.score = "needs_review", None, None
        role.reason = "Processing could not be completed for this role. Review the original CV."

    def _begin(self, lease: Lease, resume_id: UUID, config: dict[str, Any]) -> UUID | None:
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None and upload.application_id
            advisory_lock(session, f"evaluation:{upload.application_id}")
            app = session.scalar(
                select(Application).where(Application.id == upload.application_id).with_for_update()
            )
            assert app is not None
            if app.current_resume_id != resume_id:
                upload.status, upload.review_reason = "completed", "SUPERSEDED_CV"
                return None
            if app.rejected_at:
                raise ProviderError("APPLICATION_CLOSED")
            saved = session.scalar(
                select(ReviewDecision.id)
                .where(
                    ReviewDecision.application_id == app.id,
                    (
                        ReviewDecision.match_id.in_(
                            select(MatchRun.id).where(MatchRun.resume_id == resume_id)
                        )
                    )
                    | (
                        ReviewDecision.evaluation_id.in_(
                            select(MatchingEvaluation.id).where(
                                MatchingEvaluation.resume_id == resume_id
                            )
                        )
                    ),
                )
                .limit(1)
            )
            if saved:
                upload.status = "completed"
                return None
            roles = active_roles(session)
            signature = hashlib.sha256(
                json.dumps(
                    {
                        "roles": roles,
                        "job": str(lease.id),
                        "policy": POLICY_VERSION,
                        "config": config,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            evaluation = session.scalar(
                select(MatchingEvaluation).where(
                    MatchingEvaluation.resume_id == resume_id,
                    MatchingEvaluation.signature == signature,
                )
            )
            if evaluation is None:
                evaluation = MatchingEvaluation(
                    id=uuid4(),
                    tenant_id=lease.tenant_id,
                    application_id=app.id,
                    resume_id=resume_id,
                    signature=signature,
                    role_snapshot=roles,
                    config={
                        **config,
                        "matching_policy_version": POLICY_VERSION,
                        "experience_model": self.providers.experience.model
                        if self.providers.experience
                        else None,
                        "experience_prompt": getattr(
                            self.providers.experience, "prompt", "experience-v1"
                        ),
                        "as_of": upload.source_received_at.isoformat(),
                    },
                )
                session.add(evaluation)
                session.flush()
                for row in roles:
                    session.add(
                        RoleAssessment(
                            tenant_id=lease.tenant_id,
                            application_id=app.id,
                            evaluation_id=evaluation.id,
                            opening_id=UUID(row["opening_id"]),
                            criteria_version_id=UUID(row["version_id"])
                            if row["version_id"]
                            else None,
                        )
                    )
                # A new evaluation supersedes incomplete batch inputs, never stored vectors.
                session.execute(delete(EmbeddingBatch).where(EmbeddingBatch.upload_id == upload.id))
                app.latest_evaluation_id, app.latest_match_id = evaluation.id, None
                app.status, app.lock_version = "processing", app.lock_version + 1
            if evaluation.config.get("experience_model") != (
                self.providers.experience.model if self.providers.experience else None
            ):
                raise ProviderError("PROCESSING_CONFIG_CHANGED")
            if evaluation.state == "completed":
                return None
            upload.status = "matching"
            if upload.review_reason not in {"RESUME_UPDATED", "REAPPLICATION"}:
                upload.review_reason = None
            return evaluation.id

    def _experience(
        self,
        lease: Lease,
        evaluation_id: UUID,
        extraction: Extraction,
        profile: CandidateProfile,
    ) -> tuple[ExperienceFacts, list[ExperienceSource]]:
        with self.queue.owned(lease) as session:
            evaluation = session.get(MatchingEvaluation, evaluation_id)
            assert evaluation is not None
            if evaluation.experience_facts:
                return (
                    ExperienceFacts.model_validate(evaluation.experience_facts["facts"]),
                    [
                        ExperienceSource.model_validate(item)
                        for item in evaluation.experience_facts["sources"]
                    ],
                )
            roles = evaluation.role_snapshot
            identities = [profile.name or "", profile.email or "", profile.mobile or ""]
            sources = [
                source
                for i, page in enumerate(extraction.pages)
                for source in source_lines(
                    f"cv:{i}", page.text, page=page.number, identities=identities
                )
            ]
            for row in roles:
                opening = session.get(Opening, UUID(row["opening_id"]))
                assert opening is not None
                citations: dict[str, list[str]] = {}
                if row["version_id"]:
                    for entries in session.scalars(
                        select(JobCriterion.source_evidence).where(
                            JobCriterion.version_id == UUID(row["version_id"])
                        )
                    ):
                        for entry in entries:
                            key, quote = entry.get("source_id"), entry.get("quote")
                            if isinstance(key, str) and isinstance(quote, str) and quote.strip():
                                citations.setdefault(key, []).append(quote)
                sources.extend(
                    opening_experience_sources(
                        f"role:{opening.id}:jd",
                        opening.job_description,
                        str(opening.id),
                        citations.get("description", []) if citations else None,
                        primary=True,
                    )
                )
                for key in (
                    "total_experience_years",
                    "relevant_experience_years",
                    "domain_experience",
                    "candidate_requirements",
                ):
                    value = opening.details.get(key)
                    if value is not None:
                        sources.extend(
                            source_lines(
                                f"role:{opening.id}:details:{key}",
                                f"{key.replace('_', ' ')}: {value}",
                                opening_id=str(opening.id),
                            )
                        )
                for document in session.scalars(
                    select(OpeningDocument).where(OpeningDocument.opening_id == opening.id)
                ):
                    for i, page in enumerate(document.extracted_pages):
                        sources.extend(
                            opening_experience_sources(
                                f"role:{opening.id}:document:{document.id}:{i}",
                                page["text"],
                                str(opening.id),
                                citations.get(f"document:{document.id}:page:{i}", [])
                                if citations
                                else None,
                            )
                        )
        provider = self.providers.experience
        if provider is None:
            raise ProviderError("EXPERIENCE_NOT_CONFIGURED")
        ids = [row["opening_id"] for row in roles]
        facts = (
            provider.extract(sources, ids)
            if ids
            else ExperienceFacts(roles=[], employment=[], complete_employment=False)
        )
        validate_facts(facts, sources, ids)
        # Explicit structured minimums are backend-owned and cannot be omitted by a model.
        with self.queue.owned(lease) as session:
            for role in facts.roles:
                opening = session.get(Opening, UUID(role.opening_id))
                assert opening is not None
                for kind, key in (
                    ("total", "total_experience_years"),
                    ("relevant", "relevant_experience_years"),
                ):
                    amount = opening.details.get(key)
                    if amount is None:
                        continue
                    minimum = ExperienceMinimum(
                        kind=cast(Literal["total", "relevant"], kind),
                        amount=Decimal(str(amount)),
                        unit="years",
                        confidence=1,
                        evidence_ids=[
                            source.id
                            for source in sources
                            if source.id.startswith(f"role:{opening.id}:details:{key}:")
                        ],
                    )
                    if any(
                        item.kind == kind and item.required and item.months() != minimum.months()
                        for item in role.requirements
                    ):
                        role.conflicting = True
                    role.requirements.append(minimum)
            evaluation = session.get(MatchingEvaluation, evaluation_id)
            assert evaluation is not None
            evaluation.config = {
                **evaluation.config,
                "experience_prompt": getattr(provider, "prompt", "experience-v1"),
            }
            evaluation.experience_facts = {
                "facts": facts.model_dump(mode="json"),
                "sources": [item.model_dump() for item in sources],
            }
        return facts, sources

    def _gate(
        self,
        lease: Lease,
        evaluation_id: UUID,
        facts: ExperienceFacts,
        sources: list[ExperienceSource],
        profile: CandidateProfile,
    ) -> list[UUID]:
        versions = []
        with self.queue.owned(lease) as session:
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None
            by_id = {role.opening_id: role for role in facts.roles}
            for role in session.scalars(
                select(RoleAssessment).where(RoleAssessment.evaluation_id == evaluation_id)
            ):
                if role.state in {"completed", "failed"}:
                    continue
                if role.criteria_version_id is None:
                    self._failed(role, ProviderError("JOB_REQUIREMENTS_FAILED"))
                    continue
                result = eligibility(
                    by_id[str(role.opening_id)],
                    facts,
                    sources,
                    upload.source_received_at.date(),
                    profile.confidence,
                )
                role.eligibility, role.confidence, role.reason = (
                    result.model_dump(mode="json"),
                    Decimal(str(result.confidence)),
                    result.reason,
                )
                if result.status in {"failed", "unknown"}:
                    role.state = "completed"
                    role.recommendation = (
                        "recommended_rejection" if result.status == "failed" else "needs_review"
                    )
                else:
                    criteria = list(
                        session.scalars(
                            select(JobCriterion).where(
                                JobCriterion.version_id == role.criteria_version_id
                            )
                        )
                    )
                    if result.required_total_months or result.required_relevant_months:
                        criteria = [
                            item for item in criteria if not minimum_experience_criterion(item.text)
                        ]
                    if not criteria:
                        role.score, role.recommendation, role.state = (
                            Decimal(100),
                            "recommended_shortlist",
                            "completed",
                        )
                        role.reason = (
                            "Documented experience meets the requirements; "
                            "no additional criteria were defined."
                        )
                    else:
                        versions.append(role.criteria_version_id)
        return versions

    def _grade(
        self,
        lease: Lease,
        evaluation_id: UUID,
        assessment_id: UUID,
        resume_id: UUID,
        profile: CandidateProfile,
        config: dict[str, Any],
        document: FullTextDocument | None,
    ) -> None:
        with self.queue.owned(lease) as session:
            role = session.get(RoleAssessment, assessment_id)
            assert role is not None and role.criteria_version_id
            version = session.get(CriteriaVersion, role.criteria_version_id)
            assert version is not None
            threshold, version_id = version.shortlist_threshold, version.id
            checkpoints = dict(role.grade_checkpoints)
            criteria = list(
                session.scalars(
                    select(JobCriterion)
                    .where(JobCriterion.version_id == version_id)
                    .order_by(JobCriterion.position)
                )
            )
            if role.eligibility.get("required_total_months") or role.eligibility.get(
                "required_relevant_months"
            ):
                criteria = [
                    item for item in criteria if not minimum_experience_criterion(item.text)
                ]
            criterion_data = [
                (
                    item.id,
                    Criterion(
                        text=item.text,
                        required=item.required,
                        strict=item.strict,
                        weight=float(item.weight),
                    ),
                )
                for item in criteria
            ]
            rubric = (
                FullTextRubric(
                    lease.tenant_id,
                    version.opening_id,
                    version.source_revision,
                    version.id,
                    tuple(
                        RubricCriterion(str(item.id), item.text, item.required, item.strict)
                        for item in criteria
                    ),
                )
                if document is not None
                else None
            )
            supplied = (
                self._rag_inputs(session, lease.tenant_id, resume_id, criteria)
                if rubric is None
                else []
            )
        grades: list[EvidenceGrade | FullTextGrade] = []
        if rubric is not None and document is not None:
            grades.extend(
                self._grade_full_text(
                    lease, assessment_id, resume_id, rubric, document, checkpoints
                )
            )
        else:
            for (identifier, criterion), inputs in zip(criterion_data, supplied, strict=True):
                key = str(identifier)
                if key in checkpoints:
                    grade = EvidenceGrade.model_validate(checkpoints[key]["grade"])
                    validate_evidence(grade, inputs)
                else:
                    grade = self.providers.grader.grade(criterion.text, inputs)
                    validate_evidence(grade, inputs)
                    with self.queue.owned(lease) as session:
                        evidence = self._evidence(session, grade, resume_id)
                        role = session.get(RoleAssessment, assessment_id)
                        assert role is not None
                        checkpoints[key] = {"grade": grade.model_dump(), "evidence": evidence}
                        role.grade_checkpoints = dict(checkpoints)
                grades.append(grade)
        is_full_text = document is not None
        policy_version = FULL_TEXT_POLICY_VERSION if is_full_text else POLICY_VERSION
        result = calculate_verified_match(
            [criterion for _, criterion in criterion_data], grades, threshold
        )
        with self.queue.owned(lease) as session:
            role = session.get(RoleAssessment, assessment_id)
            assert role is not None
            opening = session.scalar(
                select(Opening).where(Opening.id == role.opening_id).with_for_update()
            )
            latest_version = session.scalar(
                select(CriteriaVersion.id)
                .where(CriteriaVersion.opening_id == role.opening_id)
                .order_by(CriteriaVersion.version.desc())
                .limit(1)
            )
            if not opening or opening.status != "active":
                raise ProviderError(
                    "OPENING_CLOSED"
                    if opening and opening.status == "closed"
                    else "OPENING_NOT_ACTIVE"
                )
            if opening.criteria_status != "ready" or latest_version != role.criteria_version_id:
                raise ProviderError("JOB_REQUIREMENTS_CHANGED")
            upload = session.get(CVUpload, lease.aggregate_id)
            assert upload is not None
            if upload.review_reason in {"RESUME_UPDATED", "REAPPLICATION"}:
                result = replace(
                    result,
                    recommendation="needs_review",
                    reason="A new CV or re-application requires recruiter review.",
                )
            confidence = min(
                [
                    profile.confidence,
                    float(role.confidence or 0),
                    *[grade.confidence for grade in grades],
                ]
            )
            run = session.scalar(
                select(MatchRun).where(
                    MatchRun.resume_id == resume_id,
                    MatchRun.criteria_version_id == version_id,
                    MatchRun.policy_version == policy_version,
                )
            )
            if run is None:
                evaluation = session.get(MatchingEvaluation, evaluation_id)
                assert evaluation is not None
                run = MatchRun(
                    id=uuid4(),
                    tenant_id=lease.tenant_id,
                    application_id=evaluation.application_id,
                    resume_id=resume_id,
                    criteria_version_id=version_id,
                    recommendation=result.recommendation,
                    score=result.score,
                    reason=result.reason,
                    model=config["actual_grader_model"],
                    provider=self.providers.name,
                    prompt_version=config["grading_prompt"],
                    scoring_strategy="full_text" if is_full_text else "rag",
                    embedding_model=None if is_full_text else self.providers.embeddings.model,
                    embedding_dimensions=None if is_full_text else 1536,
                    embedding_fingerprint=None if is_full_text else self.providers.fingerprint,
                    policy_version=policy_version,
                    processing_config={
                        **config,
                        "matching_policy_version": POLICY_VERSION,
                        "evidence_validation_version": "document-offsets-v1"
                        if is_full_text
                        else getattr(
                            self.providers.grader, "evidence_validation_version", "strict-exact-v1"
                        ),
                    },
                    missing_required_criteria=[
                        str(criterion_data[i][0]) for i in result.missing_required
                    ],
                )
                session.add(run)
                session.flush()
                for (identifier, _), graded in zip(criterion_data, grades, strict=True):
                    session.add(
                        CriterionResult(
                            tenant_id=lease.tenant_id,
                            match_id=run.id,
                            criterion_id=identifier,
                            status=graded.status,
                            confidence=Decimal(str(graded.confidence)),
                            reason=graded.reason,
                            evidence=(
                                self._evidence(session, graded, resume_id)
                                if isinstance(graded, EvidenceGrade)
                                else checkpoints[str(identifier)]["evidence"]
                            ),
                        )
                    )
            role.match_run_id, role.score = run.id, result.score
            role.provisional, role.recommendation = result.provisional, result.recommendation
            role.reason, role.confidence = result.reason, Decimal(str(confidence))
            role.state, role.failure_code = "completed", None

    def _rag_inputs(
        self, session: Session, tenant_id: UUID, resume_id: UUID, criteria: list[JobCriterion]
    ) -> list[list[EvidenceInput]]:
        scoped = (
            select(ResumeChunk)
            .where(
                ResumeChunk.tenant_id == tenant_id,
                ResumeChunk.resume_id == resume_id,
                ResumeChunk.embedding_fingerprint == self.providers.fingerprint,
                ResumeChunk.embedding_model == self.providers.embeddings.model,
            )
            .cte("scoped_chunks")
            .prefix_with("MATERIALIZED")
        )
        chunk_alias = aliased(ResumeChunk, scoped)
        chunks = list(session.scalars(select(chunk_alias).order_by(chunk_alias.position)))
        ranked = [RankedChunk(str(item.id), item.position, item.scoring_text) for item in chunks]
        supplied = []
        for item in criteria:
            if (
                item.embedding is None
                or item.embedding_fingerprint != self.providers.fingerprint
                or item.embedding_model != self.providers.embeddings.model
            ):
                raise ProviderError("EMBEDDING_MODEL_MISMATCH")
            semantic = list(
                session.scalars(
                    select(chunk_alias)
                    .order_by(chunk_alias.embedding.cosine_distance(item.embedding))
                    .limit(self.queue.settings.retrieval_candidates_per_method)
                )
            )
            selected = evidence_selection(
                ranked,
                [str(chunk.id) for chunk in semantic],
                keyword_rank(ranked, item.text, self.queue.settings.retrieval_candidates_per_method)
                if self.queue.settings.retrieval_mode == "hybrid"
                else [],
                top_k=self.queue.settings.retrieval_top_k,
                neighbor_window=self.queue.settings.retrieval_neighbor_window,
                budget=self.queue.settings.retrieval_context_tokens,
                rrf_k=self.queue.settings.retrieval_rrf_k,
            )
            if not selected:
                raise ProviderError("EVIDENCE_BUDGET_EXCEEDED")
            supplied.append(selected)
        return supplied

    def _grade_full_text(
        self,
        lease: Lease,
        assessment_id: UUID,
        resume_id: UUID,
        rubric: FullTextRubric,
        document: FullTextDocument,
        checkpoints: dict[str, Any],
    ) -> list[FullTextGrade]:
        if all(criterion.criterion_id in checkpoints for criterion in rubric.criteria):
            response = FullTextResponse(
                grades=[
                    FullTextGrade.model_validate(checkpoints[criterion.criterion_id]["grade"])
                    for criterion in rubric.criteria
                ]
            )
            return verify_full_text(rubric, document, response).grades
        with self.queue.owned(lease) as session:
            completed = session.scalar(
                select(MatchRun).where(
                    MatchRun.resume_id == resume_id,
                    MatchRun.criteria_version_id == rubric.version_id,
                    MatchRun.policy_version == FULL_TEXT_POLICY_VERSION,
                )
            )
            saved = (
                list(
                    session.scalars(
                        select(CriterionResult).where(CriterionResult.match_id == completed.id)
                    )
                )
                if completed
                else []
            )
        if saved:
            response = FullTextResponse(
                grades=[
                    FullTextGrade(
                        criterion_id=str(item.criterion_id),
                        status=cast(Grade, item.status),
                        confidence=float(item.confidence),
                        reason=item.reason,
                        citations=[
                            QuoteSelection(source_id=citation["source_id"], quote=citation["quote"])
                            for citation in item.evidence
                        ],
                    )
                    for item in saved
                ]
            )
            result = verify_full_text(rubric, document, response)
        else:
            grader = self.providers.full_text_grader
            if grader is None:
                raise ProviderError("FULL_TEXT_GRADER_NOT_CONFIGURED")
            result = grader.grade(rubric, document)
        with self.queue.owned(lease) as session:
            role = session.get(RoleAssessment, assessment_id)
            assert role is not None
            checkpoints.update(
                {
                    grade.criterion_id: {
                        "grade": grade.model_dump(),
                        "evidence": [
                            value.model_dump() for value in result.evidence[grade.criterion_id]
                        ],
                    }
                    for grade in result.grades
                }
            )
            role.grade_checkpoints = dict(checkpoints)
        return result.grades

    def _evidence(
        self, session: Session, grade: EvidenceGrade, resume_id: UUID
    ) -> list[dict[str, Any]]:
        result = []
        for identifier, quote in zip(grade.evidence_chunk_ids, grade.quotes, strict=True):
            chunk = session.get(ResumeChunk, UUID(identifier))
            if (
                not chunk
                or chunk.resume_id != resume_id
                or chunk.embedding_fingerprint != self.providers.fingerprint
            ):
                raise ProviderError("INVALID_EVIDENCE")
            start = chunk.scoring_text.find(quote)
            if start < 0 or chunk.text[start : start + len(quote)] != quote:
                raise ProviderError("INVALID_EVIDENCE")
            result.append(
                {
                    "chunk_id": identifier,
                    "quote": quote,
                    "chunk_char_start": start,
                    "chunk_char_end": start + len(quote),
                    "source_spans": chunk.source_spans,
                }
            )
        return result

    def _publish(
        self,
        lease: Lease,
        evaluation_id: UUID,
        resume_id: UUID,
        profile: CandidateProfile,
        strategy: ScoringStrategy,
    ) -> None:
        with self.queue.owned(lease) as session:
            evaluation = session.get(MatchingEvaluation, evaluation_id)
            assert evaluation is not None
            app = session.scalar(
                select(Application)
                .where(Application.id == evaluation.application_id)
                .with_for_update()
            )
            upload = session.get(CVUpload, lease.aggregate_id)
            policy = session.scalar(select(RetentionSetting))
            assert app is not None and upload is not None and policy is not None
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
                    "APPLICATION_CLOSED" if app.rejected_at else "RETENTION_EXPIRED"
                )
            if app.current_resume_id != resume_id or app.latest_evaluation_id != evaluation.id:
                evaluation.state = "superseded"
                upload.status, upload.review_reason = "completed", "SUPERSEDED_CV"
                return
            current_roles = active_roles(session)
            if current_roles != evaluation.role_snapshot:
                evaluation.state = "superseded"
                upload.status = "queued"
                digest = hashlib.sha256(
                    json.dumps(current_roles, sort_keys=True).encode()
                ).hexdigest()
                emit(
                    session,
                    lease.tenant_id,
                    "cv.uploaded",
                    upload.id,
                    f"cv.role-set:{evaluation.id}:{digest}",
                    scoring_strategy=strategy,
                )
                return
            roles = list(
                session.scalars(
                    select(RoleAssessment)
                    .where(RoleAssessment.evaluation_id == evaluation.id)
                    .order_by(RoleAssessment.opening_id)
                )
            )
            ranked = sorted(
                [role for role in roles if role.score is not None],
                key=lambda item: (-float(item.score or 0), str(item.opening_id)),
            )
            qualifying = [role for role in ranked if role.recommendation == "recommended_shortlist"]
            best = qualifying[0] if qualifying else (ranked[0] if ranked else None)
            failed = [role for role in roles if role.failure_code]
            if not roles:
                recommendation, reason = "needs_review", "There are no active openings to evaluate."
            elif failed or any(role.recommendation == "needs_review" for role in roles):
                recommendation, reason = (
                    "needs_review",
                    "Experience or criterion evidence requires recruiter review.",
                )
            elif (
                len(qualifying) > 1
                and float(qualifying[0].score or 0) - float(qualifying[1].score or 0) <= 5
            ):
                recommendation, reason = (
                    "needs_review",
                    "Two qualifying roles are within five points. Select the appropriate role.",
                )
            elif qualifying:
                recommendation, reason = (
                    "recommended_shortlist",
                    "The highest-scoring eligible role meets the approved requirements.",
                )
            else:
                recommendation = "recommended_rejection"
                reason = (
                    roles[0].reason
                    if len(roles) == 1
                    else "No active role meets the documented experience and matching requirements."
                )
            evaluation.recommendation, evaluation.reason = recommendation, reason
            evaluation.recommended_assessment_id = best.id if best else None
            evaluation.confidence = (
                None
                if failed or not roles
                else Decimal(
                    str(min([profile.confidence, *[float(role.confidence or 0) for role in roles]]))
                )
            )
            evaluation.state, evaluation.processed_at = "completed", utcnow()
            app.status, app.latest_match_id = "needs_review", best.match_run_id if best else None
            app.lock_version += 1
            upload.status = "needs_review" if failed else "completed"
            if upload.review_reason not in {"RESUME_UPDATED", "REAPPLICATION"}:
                upload.review_reason = failed[0].failure_code if failed else None
            if not roles:
                source_opening = session.get(Opening, upload.opening_id)
                if source_opening and source_opening.status == "closed":
                    upload.status, upload.review_reason = "needs_review", "OPENING_CLOSED"
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
