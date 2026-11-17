from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, SecretStr, model_validator

from recruitment.domain.contracts import MatchResult
from recruitment.domain.openings import JobDetails, OpeningStatus


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Schema):
    email: EmailStr
    password: SecretStr = Field(min_length=12, max_length=128)


class UserRead(Schema):
    user_id: UUID


class SessionRead(Schema):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class TenantCreate(Schema):
    name: str = Field(min_length=1, max_length=200)


class TenantRead(Schema):
    id: UUID
    name: str
    timezone: Literal["Asia/Kolkata"]
    role: Literal["recruiter"]


class MemberCreate(Schema):
    user_id: UUID


class MemberRead(Schema):
    user_id: UUID
    role: Literal["recruiter"]


class MembershipRead(Schema):
    tenant_id: UUID
    name: str
    role: Literal["recruiter"]


class MeRead(UserRead):
    memberships: list[MembershipRead]


class OpeningDocumentRead(Schema):
    id: UUID
    file_name: str
    mime_type: str
    byte_length: int
    created_at: datetime


class OpeningRead(Schema):
    id: UUID
    title: str
    job_description: str
    status: OpeningStatus
    details: JobDetails
    questions: list[str]
    revision: int
    source_revision: int
    documents: list[OpeningDocumentRead]


class UploadRead(Schema):
    upload_id: UUID
    status: str
    application_id: UUID | None
    review_reason: str | None
    retention_expires_at: datetime


class OpeningList(Schema):
    items: list[OpeningRead]
    limit: int
    offset: int


class UploadList(Schema):
    items: list[UploadRead]
    limit: int
    offset: int


class ApplicationRead(Schema):
    id: UUID
    candidate_id: UUID
    opening_id: UUID
    profile: dict[str, Any]
    status: str
    latest_match_id: UUID | None
    retention_expires_at: datetime
    retention_display_ist: str
    version: int
    latest_evaluation_id: UUID | None = None
    recruiter_decision: dict[str, Any] | None = None


class LegacyMatchRead(Schema):
    match_run_id: UUID
    score: float | None
    reason: str
    recommendation: Literal["recommended_shortlist", "recommended_rejection", "needs_review"]


class RoleAssessmentRead(Schema):
    id: UUID
    opening_id: UUID
    opening_title: str
    opening_status: str
    criteria_version_id: UUID | None
    state: str
    match_result: MatchResult | None
    recommendation: Literal["recommended_shortlist", "recommended_rejection", "needs_review"]
    reason: str
    confidence: float | None
    score: float | None
    score_provisional: bool
    match_run_id: UUID | None
    failure_code: str | None


class EvaluationRead(Schema):
    id: UUID
    resume_id: UUID
    state: str
    recommendation: Literal["recommended_shortlist", "recommended_rejection", "needs_review"]
    reason: str
    confidence: float | None
    recommended_assessment_id: UUID | None
    processed_at: datetime | None
    processing_config: dict[str, Any]
    roles: list[RoleAssessmentRead]


class AssessmentDetail(Schema):
    assessment: RoleAssessmentRead
    result: MatchResult | LegacyMatchRead | None


class MatchResponse(Schema):
    status: str
    result: MatchResult | LegacyMatchRead | None
    evaluation: EvaluationRead | None = None


class ReviewCreate(Schema):
    match_run_id: UUID | None = None
    evaluation_id: UUID | None = None
    expected_version: int | None = Field(default=None, ge=1)
    role_assessment_id: UUID | None = None
    decision: Literal["shortlist", "reject"]
    confirm_rejection: bool = False
    reason: str | None = Field(default=None, min_length=5, max_length=1000)

    @model_validator(mode="after")
    def current_reference(self) -> "ReviewCreate":
        if bool(self.match_run_id) == bool(self.evaluation_id):
            raise ValueError("Provide one current evaluation or legacy match reference.")
        if self.evaluation_id and self.expected_version is None:
            raise ValueError("The application version is required.")
        if self.evaluation_id and self.decision == "shortlist" and self.role_assessment_id is None:
            raise ValueError("Select one role.")
        return self


class ReviewRead(Schema):
    id: UUID
    decision: Literal["shortlist", "reject"]
    status: str | None = None
    retention_expires_at: datetime | None = None


class ReviewApplication(Schema):
    id: UUID
    candidate_id: UUID
    name: str | None
    upload_id: UUID | None
    status: str
    latest_match_id: UUID | None


class ReviewQueue(Schema):
    uploads: list[UploadRead]
    applications: list[ReviewApplication]
    identity_review_ids: list[UUID] = Field(default_factory=list)
    limit: int
    offset: int


class RetentionRead(Schema):
    rejected_retention_days: int
    other_candidate_retention_days: int


class RetentionPatch(Schema):
    rejected_retention_days: int | None = Field(default=None, ge=1, le=3650)
    other_candidate_retention_days: int | None = Field(default=None, ge=1, le=3650)


class ErrorDetails(Schema):
    code: str
    message: str
    request_id: str
    details: list[dict[str, Any]] = Field(default_factory=list)


class ErrorResponse(Schema):
    error: ErrorDetails


class OverviewCounts(Schema):
    cvs_received: int
    processed: int
    shortlisted: int
    needs_review: int
    rejected: int


class OpeningOverview(Schema):
    opening_id: UUID
    title: str
    counts: OverviewCounts


class DailyIntake(Schema):
    date: str
    cvs_received: int


class OverviewRead(Schema):
    totals: OverviewCounts
    by_opening: list[OpeningOverview]
    daily_intake: list[DailyIntake]
    timezone: Literal["Asia/Kolkata"]
    scope: Literal["retained_records"]
    days: int
