"""Mailbox intake and processing checkpoints. All records have business ownership."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from recruitment.adapters.db.models import Base, Owned


class Mailbox(Owned, Base):
    __tablename__ = "mailbox_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "opening_id"], ["job_openings.tenant_id", "job_openings.id"]
        ),
        Index(
            "uq_active_mailbox_owner",
            "provider",
            "owner_hash",
            unique=True,
            postgresql_where=text("status NOT IN ('disconnected') AND owner_hash IS NOT NULL"),
        ),
        Index("ix_mailbox_schedule", "next_sync_at", "next_maintenance_at"),
        Index(
            "uq_active_mailbox_address",
            "address_hash",
            unique=True,
            postgresql_where=text("status <> 'disconnected' AND address_hash IS NOT NULL"),
        ),
    )
    provider: Mapped[str]
    connection_method: Mapped[str] = mapped_column(default="gmail_oauth")
    address_hash: Mapped[str | None]
    recruiter_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    opening_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(default="pending_authorization")
    email: Mapped[str | None]
    owner_hash: Mapped[str | None]
    notification_lookup: Mapped[str | None]
    folder_id: Mapped[str] = mapped_column(default="INBOX")
    intake_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    generation: Mapped[int] = mapped_column(default=1)
    source_period: Mapped[int] = mapped_column(default=1)
    sync_status: Mapped[str] = mapped_column(default="idle")
    failure_code: Mapped[str | None]
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_maintenance_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    subscription_id: Mapped[str | None]
    subscription_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notification_secret_hash: Mapped[str | None]
    lease_token: Mapped[UUID | None]
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MailboxCredential(Owned, Base):
    __tablename__ = "mailbox_credentials"
    __table_args__ = (
        UniqueConstraint("connection_id"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["mailbox_connections.tenant_id", "mailbox_connections.id"],
            ondelete="CASCADE",
        ),
    )
    connection_id: Mapped[UUID]
    key_id: Mapped[str]
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)


class OAuthAttempt(Owned, Base):
    __tablename__ = "mailbox_oauth_attempts"
    __table_args__ = (
        UniqueConstraint("state_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["mailbox_connections.tenant_id", "mailbox_connections.id"],
            ondelete="CASCADE",
        ),
    )
    connection_id: Mapped[UUID]
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    state_hash: Mapped[str]
    key_id: Mapped[str]
    nonce: Mapped[bytes] = mapped_column(LargeBinary)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MailboxReceipt(Owned, Base):
    __tablename__ = "mailbox_notification_receipts"
    __table_args__ = (
        UniqueConstraint("connection_id", "receipt_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["mailbox_connections.tenant_id", "mailbox_connections.id"],
            ondelete="CASCADE",
        ),
    )
    connection_id: Mapped[UUID]
    receipt_hash: Mapped[str]


class MailMessage(Owned, Base):
    __tablename__ = "mail_source_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("connection_id", "provider_message_id"),
        ForeignKeyConstraint(
            ["tenant_id", "connection_id"],
            ["mailbox_connections.tenant_id", "mailbox_connections.id"],
            ondelete="CASCADE",
        ),
        Index("ix_mail_message_retention", "tenant_id", "received_at"),
        Index("ix_mail_message_thread", "connection_id", "provider_thread_id"),
    )
    connection_id: Mapped[UUID]
    provider_message_id: Mapped[str]
    internet_message_id: Mapped[str | None]
    provider_thread_id: Mapped[str | None]
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    generation: Mapped[int]


class MailAttachment(Owned, Base):
    __tablename__ = "mail_source_attachments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("message_id", "provider_attachment_id"),
        ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["mail_source_messages.tenant_id", "mail_source_messages.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["cv_uploads.tenant_id", "cv_uploads.id"],
            ondelete="CASCADE",
        ),
    )
    message_id: Mapped[UUID]
    provider_attachment_id: Mapped[str]
    filename: Mapped[str]
    mime_type: Mapped[str]
    byte_length: Mapped[int]
    upload_id: Mapped[UUID | None]
    status: Mapped[str] = mapped_column(default="queued")
    failure_code: Mapped[str | None]


class DocumentDerivative(Owned, Base):
    __tablename__ = "document_derivatives"
    __table_args__ = (
        UniqueConstraint("upload_id"),
        ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["cv_uploads.tenant_id", "cv_uploads.id"],
            ondelete="CASCADE",
        ),
    )
    upload_id: Mapped[UUID]
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str]
    converter_version: Mapped[str]


class ContactRevision(Owned, Base):
    __tablename__ = "candidate_contact_revisions"
    __table_args__ = (
        UniqueConstraint("candidate_id", "revision"),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
    )
    candidate_id: Mapped[UUID]
    revision: Mapped[int]
    actor_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    before: Mapped[dict[str, Any]] = mapped_column(JSON)
    after: Mapped[dict[str, Any]] = mapped_column(JSON)
    reason: Mapped[str]


class IdentityReview(Owned, Base):
    __tablename__ = "candidate_identity_reviews"
    __table_args__ = (
        UniqueConstraint("upload_id", "target_opening_id"),
        Index(
            "uq_identity_upload",
            "upload_id",
            unique=True,
            postgresql_where=text("kind <> 'cross_role_submission'"),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "target_opening_id"], ["job_openings.tenant_id", "job_openings.id"]
        ),
        ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["cv_uploads.tenant_id", "cv_uploads.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["mail_source_messages.tenant_id", "mail_source_messages.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["candidates.tenant_id", "candidates.id"],
            ondelete="CASCADE",
        ),
    )
    upload_id: Mapped[UUID | None]
    target_opening_id: Mapped[UUID | None]
    message_id: Mapped[UUID | None]
    candidate_id: Mapped[UUID | None]
    kind: Mapped[str] = mapped_column(default="identity_conflict")
    status: Mapped[str] = mapped_column(default="open")
    proposed_contacts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidate_options: Mapped[list[str]] = mapped_column(JSON, default=list)
    decision: Mapped[str | None]
    reason: Mapped[str | None]
    actor_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
