from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from recruitment.adapters.db.matching import JobScoringRubric
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import JobDescription
from recruitment.domain.scoring import checklist, signature
from recruitment.domain.scoring_contracts import RUBRIC_PROMPT, ScoringRubric
from recruitment.ports.providers import ChecklistBuilder
from recruitment.services.common import advisory_lock


def saved_rubric(session: Session, job: JobDescription) -> ScoringRubric | None:
    row = session.scalar(
        select(JobScoringRubric).where(JobScoringRubric.signature == signature(job))
    )
    return ScoringRubric.model_validate(row.content) if row else None


def prepare_rubric(
    session: Session, builder: ChecklistBuilder | None, job: JobDescription, tenant_id: UUID
) -> ScoringRubric:
    key = signature(job)
    advisory_lock(session, f"scoring-rubric:{tenant_id}:{key}")
    existing = saved_rubric(session, job)
    if existing:
        return existing
    if builder is None:
        raise ProviderError("SCORING_RUBRIC_UNAVAILABLE")
    rubric = checklist(job, builder.prepare(job))
    session.add(
        JobScoringRubric(
            tenant_id=tenant_id,
            signature=key,
            content=rubric.model_dump(),
            model=builder.model,
            prompt_version=RUBRIC_PROMPT,
        )
    )
    session.flush()
    return rubric
