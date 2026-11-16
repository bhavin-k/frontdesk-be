from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    column,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from recruitment.core.time import utcnow


class Base(DeclarativeBase):
    type_annotation_map = {str: Text()}


class Record:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Owned(Record):
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"))


class User(Record, Base):
    __tablename__ = "users"
    __table_args__ = (Index("users_email_normalized", func.lower(column("email")), unique=True),)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    # Better Auth credentials live in better_auth_accounts; API-only accounts keep this hash.
    password_hash: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, default="", server_default="")
    email_verified: Mapped[bool] = mapped_column(default=False, server_default="false")
    image: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Record, Base):
    __tablename__ = "auth_sessions"
    __table_args__ = (Index("ix_auth_sessions_user", "user_id", "expires_at"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Record, Base):
    __tablename__ = "login_attempts"
    __table_args__ = (Index("ix_login_attempts_identity", "identity_hash", "created_at"),)
    identity_hash: Mapped[str] = mapped_column(String(64))


class Tenant(Record, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(default="Asia/Kolkata")


class Member(Owned, Base):
    __tablename__ = "tenant_members"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(default="recruiter")


class RetentionSetting(Owned, Base):
    __tablename__ = "tenant_retention_settings"
    __table_args__ = (UniqueConstraint("tenant_id"),)
    rejected_retention_days: Mapped[int] = mapped_column(default=7)
    other_candidate_retention_days: Mapped[int] = mapped_column(default=30)


class Opening(Owned, Base):
    __tablename__ = "job_openings"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    title: Mapped[str] = mapped_column(String(200))
    job_description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(default="active")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    questions: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(default=1)
    source_revision: Mapped[int] = mapped_column(default=1)
    criteria_status: Mapped[str] = mapped_column(default="queued")
    criteria_error: Mapped[str | None]
    preparation_config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class CriteriaVersion(Owned, Base):
    __tablename__ = "job_criteria_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("opening_id", "version"),
        UniqueConstraint("opening_id", "source_revision"),
        ForeignKeyConstraint(
            ["tenant_id", "opening_id"],
            ["job_openings.tenant_id", "job_openings.id"],
            ondelete="CASCADE",
        ),
    )
    opening_id: Mapped[UUID]
    version: Mapped[int] = mapped_column(default=1)
    shortlist_threshold: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    source_revision: Mapped[int] = mapped_column(default=1)
    processing_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class JobCriterion(Owned, Base):
    __tablename__ = "job_criteria"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("version_id", "position"),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["job_criteria_versions.tenant_id", "job_criteria_versions.id"],
            ondelete="CASCADE",
        ),
    )
    version_id: Mapped[UUID]
    position: Mapped[int]
    text: Mapped[str] = mapped_column(Text)
    required: Mapped[bool]
    strict: Mapped[bool]
    weight: Mapped[Decimal] = mapped_column(Numeric(8, 3))
    source_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    embedding: Mapped[Any | None] = mapped_column(Vector(1536), nullable=True)
    embedding_model: Mapped[str | None]
    embedding_fingerprint: Mapped[str | None]


class Candidate(Owned, Base):
    __tablename__ = "candidates"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)
    profile: Mapped[dict[str, Any]] = mapped_column(JSON)
    current_source_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_source_key: Mapped[str | None]
    contact_revision: Mapped[int] = mapped_column(default=1, server_default="1")
    contact_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}"
    )


class Contact(Owned, Base):
    __tablename__ = "candidate_contacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "value"),
        UniqueConstraint("tenant_id", "candidate_id", "kind"),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
    )
    candidate_id: Mapped[UUID]
    kind: Mapped[str]
    value: Mapped[str]


class ResumeFile(Owned, Base):
    __tablename__ = "resume_files"
    __table_args__ = (UniqueConstraint("tenant_id", "id"), UniqueConstraint("tenant_id", "sha256"))
    sha256: Mapped[str] = mapped_column(String(64))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    mime_type: Mapped[str]
    byte_length: Mapped[int]


class Application(Owned, Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "id", "current_resume_id"],
            ["resumes.tenant_id", "resumes.application_id", "resumes.id"],
            name="fk_current_resume",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "opening_id"],
            ["job_openings.tenant_id", "job_openings.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "id", "latest_match_id"],
            ["match_runs.tenant_id", "match_runs.application_id", "match_runs.id"],
            name="fk_latest_match",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        Index(
            "uq_active_application",
            "tenant_id",
            "candidate_id",
            unique=True,
            postgresql_where=text("rejected_at IS NULL"),
        ),
        Index("ix_application_expiry", "tenant_id", "retention_expires_at"),
        ForeignKeyConstraint(
            ["tenant_id", "id", "latest_evaluation_id"],
            [
                "matching_evaluations.tenant_id",
                "matching_evaluations.application_id",
                "matching_evaluations.id",
            ],
            name="fk_latest_evaluation",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )
    candidate_id: Mapped[UUID]
    opening_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(default="processing")
    latest_match_id: Mapped[UUID | None]
    latest_evaluation_id: Mapped[UUID | None]
    current_resume_id: Mapped[UUID | None]
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lock_version: Mapped[int] = mapped_column(default=1)


class CVUpload(Owned, Base):
    __tablename__ = "cv_uploads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("file_id"),
        Index("ix_upload_review", "tenant_id", "status", "created_at"),
        ForeignKeyConstraint(
            ["tenant_id", "resolved_candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "file_id"],
            ["resume_files.tenant_id", "resume_files.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "opening_id"],
            ["job_openings.tenant_id", "job_openings.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
        ),
    )
    file_id: Mapped[UUID]
    opening_id: Mapped[UUID]
    application_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(default="queued")
    review_reason: Mapped[str | None]
    source_received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_candidate_id: Mapped[UUID | None]
    identity_choice: Mapped[str | None]
    retention_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Resume(Owned, Base):
    __tablename__ = "resumes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "application_id", "id"),
        UniqueConstraint("upload_id"),
        UniqueConstraint("application_id", "version"),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["cv_uploads.tenant_id", "cv_uploads.id"],
            ondelete="CASCADE",
        ),
    )
    application_id: Mapped[UUID]
    upload_id: Mapped[UUID]
    version: Mapped[int]


class ResumeExtraction(Owned, Base):
    __tablename__ = "resume_extractions"
    __table_args__ = (
        UniqueConstraint("upload_id"),
        ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["cv_uploads.tenant_id", "cv_uploads.id"],
            ondelete="CASCADE",
        ),
    )
    upload_id: Mapped[UUID]
    pages: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    method: Mapped[str]
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    profile: Mapped[dict[str, Any]] = mapped_column(JSON)
    profile_model: Mapped[str]
    prompt_version: Mapped[str]


class ResumeChunk(Owned, Base):
    __tablename__ = "resume_chunks"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("resume_id", "position"),
        Index("ix_chunks_resume", "tenant_id", "resume_id"),
        Index(
            "ix_chunk_vector",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        ForeignKeyConstraint(
            ["tenant_id", "resume_id"],
            ["resumes.tenant_id", "resumes.id"],
            ondelete="CASCADE",
        ),
    )
    resume_id: Mapped[UUID]
    position: Mapped[int]
    source_spans: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    text: Mapped[str] = mapped_column(Text)
    scoring_text: Mapped[str] = mapped_column(Text)
    token_start: Mapped[int]
    token_end: Mapped[int]
    embedding: Mapped[Any] = mapped_column(Vector(1536))
    embedding_model: Mapped[str]
    embedding_dimensions: Mapped[int] = mapped_column(default=1536)
    embedding_fingerprint: Mapped[str] = mapped_column(
        default="legacy-unfingerprinted", server_default="legacy-unfingerprinted"
    )


class MatchRun(Owned, Base):
    __tablename__ = "match_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "application_id", "id"),
        UniqueConstraint(
            "resume_id", "criteria_version_id", "policy_version", name="uq_match_policy"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "resume_id"],
            ["resumes.tenant_id", "resumes.application_id", "resumes.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "criteria_version_id"],
            ["job_criteria_versions.tenant_id", "job_criteria_versions.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
        ),
    )
    application_id: Mapped[UUID]
    resume_id: Mapped[UUID]
    criteria_version_id: Mapped[UUID]
    recommendation: Mapped[str]
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    reason: Mapped[str] = mapped_column(Text)
    model: Mapped[str]
    provider: Mapped[str]
    prompt_version: Mapped[str]
    scoring_strategy: Mapped[str] = mapped_column(server_default="rag")
    embedding_model: Mapped[str | None]
    embedding_dimensions: Mapped[int | None]
    embedding_fingerprint: Mapped[str | None]
    processing_config: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}"
    )
    policy_version: Mapped[str] = mapped_column(default="legacy-v1", server_default="legacy-v1")
    missing_required_criteria: Mapped[list[str]] = mapped_column(JSON)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CriterionResult(Owned, Base):
    __tablename__ = "criterion_results"
    __table_args__ = (
        UniqueConstraint("match_id", "criterion_id"),
        ForeignKeyConstraint(
            ["tenant_id", "match_id"],
            ["match_runs.tenant_id", "match_runs.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "criterion_id"],
            ["job_criteria.tenant_id", "job_criteria.id"],
        ),
    )
    match_id: Mapped[UUID]
    criterion_id: Mapped[UUID]
    status: Mapped[str]
    confidence: Mapped[Decimal] = mapped_column(Numeric(5, 4))
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON)


class ReviewDecision(Owned, Base):
    __tablename__ = "review_decisions"
    __table_args__ = (
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
            ["tenant_id", "evaluation_id", "role_assessment_id"],
            ["role_assessments.tenant_id", "role_assessments.evaluation_id", "role_assessments.id"],
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id"],
            ["applications.tenant_id", "applications.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "match_id"],
            ["match_runs.tenant_id", "match_runs.application_id", "match_runs.id"],
            ondelete="CASCADE",
        ),
    )
    application_id: Mapped[UUID]
    match_id: Mapped[UUID | None]
    evaluation_id: Mapped[UUID | None]
    role_assessment_id: Mapped[UUID | None]
    recruiter_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str]
    reason: Mapped[str | None] = mapped_column(Text)


class Tombstone(Base):
    __tablename__ = "candidate_data_tombstones"
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    application_id: Mapped[UUID] = mapped_column(primary_key=True)
    role_id: Mapped[UUID]
    final_status: Mapped[str]
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Outbox(Owned, Base):
    __tablename__ = "outbox_messages"
    __table_args__ = (
        Index("ix_outbox_pending", "available_at", postgresql_where=text("dispatched_at IS NULL")),
    )
    kind: Mapped[str]
    scoring_strategy: Mapped[str] = mapped_column(server_default="rag")
    aggregate_id: Mapped[UUID]
    dedupe_key: Mapped[str] = mapped_column(unique=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Job(Owned, Base):
    __tablename__ = "job_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        Index(
            "ix_job_claim",
            "available_at",
            "lease_expires_at",
            postgresql_where=text("state IN ('queued','retry_wait','running')"),
        ),
    )
    kind: Mapped[str]
    scoring_strategy: Mapped[str] = mapped_column(server_default="rag")
    aggregate_id: Mapped[UUID]
    dedupe_key: Mapped[str] = mapped_column(unique=True)
    state: Mapped[str] = mapped_column(default="queued")
    attempts: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None]


class FailedJob(Owned, Base):
    __tablename__ = "failed_jobs"
    job_id: Mapped[UUID]
    aggregate_id: Mapped[UUID]
    failure_code: Mapped[str]


class IdempotencyRecord(Owned, Base):
    __tablename__ = "idempotency_records"
    __table_args__ = (UniqueConstraint("tenant_id", "scope", "key"),)
    scope: Mapped[str]
    key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[UUID]
