"""Durable multi-role evaluations and retry checkpoints."""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKeyConstraint, Index, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from recruitment.adapters.db.models import Base, Owned


class JobScoringRubric(Owned, Base):
    __tablename__ = "job_scoring_rubrics"
    __table_args__ = (UniqueConstraint("tenant_id", "signature"),)
    signature: Mapped[str]
    content: Mapped[dict[str, Any]] = mapped_column(JSON)
    model: Mapped[str]
    prompt_version: Mapped[str]


class MatchingEvaluation(Owned, Base):
    __tablename__ = "matching_evaluations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "application_id", "id"),
        UniqueConstraint("resume_id", "signature"),
        ForeignKeyConstraint(
            ["tenant_id", "id", "recommended_assessment_id"],
            ["role_assessments.tenant_id", "role_assessments.evaluation_id", "role_assessments.id"],
            name="fk_recommended_assessment",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "resume_id"],
            ["resumes.tenant_id", "resumes.application_id", "resumes.id"],
            ondelete="CASCADE",
        ),
    )
    application_id: Mapped[UUID]
    resume_id: Mapped[UUID]
    signature: Mapped[str]
    role_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(default="processing")
    recommendation: Mapped[str] = mapped_column(default="needs_review")
    reason: Mapped[str] = mapped_column(Text, default="Matching is processing.")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    recommended_assessment_id: Mapped[UUID | None]
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoleAssessment(Owned, Base):
    __tablename__ = "role_assessments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "evaluation_id", "id"),
        UniqueConstraint("evaluation_id", "opening_id"),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "evaluation_id"],
            [
                "matching_evaluations.tenant_id",
                "matching_evaluations.application_id",
                "matching_evaluations.id",
            ],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "opening_id"], ["job_openings.tenant_id", "job_openings.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "criteria_version_id"],
            ["job_criteria_versions.tenant_id", "job_criteria_versions.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "match_run_id"],
            ["match_runs.tenant_id", "match_runs.application_id", "match_runs.id"],
            ondelete="CASCADE",
            name="fk_assessment_match",
        ),
        Index("ix_role_assessment_evaluation", "tenant_id", "evaluation_id"),
    )
    evaluation_id: Mapped[UUID]
    application_id: Mapped[UUID]
    opening_id: Mapped[UUID]
    criteria_version_id: Mapped[UUID | None]
    state: Mapped[str] = mapped_column(default="pending")
    recommendation: Mapped[str] = mapped_column(default="needs_review")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    provisional: Mapped[bool] = mapped_column(default=False)
    reason: Mapped[str] = mapped_column(Text, default="Matching is processing.")
    match_run_id: Mapped[UUID | None]
    match_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None]
