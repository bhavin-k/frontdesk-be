from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, SecretStr

from recruitment.api.schemas import Schema

Provider = Literal["gmail", "microsoft"]
ConnectionMethod = Literal["imap", "gmail_oauth", "microsoft_oauth"]


class IMAPConnect(Schema):
    opening_id: UUID
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    app_password: SecretStr = Field(min_length=16, max_length=128)


class IMAPCredentials(Schema):
    expected_generation: int = Field(ge=1)
    app_password: SecretStr = Field(min_length=16, max_length=128)


class MailboxCreate(Schema):
    provider: Provider
    opening_id: UUID


class MailboxPatch(Schema):
    expected_generation: int = Field(ge=1)
    opening_id: UUID | None = None
    folder_id: str | None = Field(default=None, min_length=1, max_length=512)
    activate: bool = False
    reauthorize: bool = False


class MailboxRead(Schema):
    id: UUID
    tenant_id: UUID
    provider: Provider
    connection_method: ConnectionMethod
    enabled: bool
    email: str | None
    status: Literal[
        "pending_authorization",
        "needs_configuration",
        "connected",
        "reauthorization_required",
        "disconnecting",
        "disconnected",
        "error",
    ]
    opening_id: UUID
    folder_id: str
    generation: int
    intake_started_at: datetime | None
    intake_started_display_ist: str | None
    sync_status: Literal["idle", "queued", "running", "retry_wait", "failed"]
    last_success_at: datetime | None
    failure_code: str | None
    stalled: bool
    provider_mode: Literal["fake", "real"]
    authorization_url: str | None = None


class MailboxList(Schema):
    items: list[MailboxRead]
    limit: int
    offset: int


class OAuthCallback(Schema):
    state: str = Field(min_length=32, max_length=512)
    code: str | None = Field(default=None, max_length=8192)
    error: str | None = Field(default=None, max_length=200)


class ContactPatch(Schema):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    reason: str = Field(min_length=5, max_length=1000)
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    mobile: str | None = Field(default=None, max_length=40)


class CandidateRead(Schema):
    id: UUID
    profile: dict[str, Any]
    contact_revision: int
    current_source_at: datetime | None
    contact_history: list[dict[str, Any]] = Field(default_factory=list)
    application_id: UUID | None = None
    status: str | None = None
    opening_id: UUID | None = None
    opening_title: str | None = None
    score: float | None = None
    no_matching_opening: bool = False
    latest_upload_id: UUID | None = None


class CandidateList(Schema):
    items: list[CandidateRead]
    limit: int
    offset: int


class IdentityResolve(Schema):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["link", "create", "accept", "dismiss"]
    candidate_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=5, max_length=1000)


class IdentityRead(Schema):
    id: UUID
    upload_id: UUID | None
    message_id: UUID | None
    candidate_id: UUID | None
    kind: str
    status: Literal["open", "resolved"]
    proposed_contacts: dict[str, Any]
    candidate_options: list[UUID]
    decision: str | None
    reason: str | None
    actor_id: UUID | None
    resolved_at: datetime | None
    candidates: list[CandidateRead] = Field(default_factory=list)


class IdentityList(Schema):
    items: list[IdentityRead]
    limit: int
    offset: int


class MailProviderCapability(Schema):
    id: Provider
    connection_method: ConnectionMethod
    configured: bool
    mailbox_type: Literal["signed_in_owner"]


class MailProviderList(Schema):
    provider_mode: Literal["fake", "real"]
    items: list[MailProviderCapability]


class FolderRead(Schema):
    id: str
    name: str


class FolderList(Schema):
    items: list[FolderRead]


class IntakeRead(Schema):
    id: UUID
    connection_id: UUID
    provider: Provider
    filename: str
    received_at: datetime
    status: str
    failure_code: str | None
    upload_id: UUID | None
    processing_status: str | None
    review_reason: str | None
    application_id: UUID | None


class IntakeList(Schema):
    items: list[IntakeRead]
    limit: int
    offset: int


class ResumeVersionRead(Schema):
    id: UUID
    version: int
    upload_id: UUID
    application_id: UUID
    status: str
    review_reason: str | None
    source_received_at: datetime
    current: bool


class ResumeVersionList(Schema):
    items: list[ResumeVersionRead]
    limit: int
    offset: int
