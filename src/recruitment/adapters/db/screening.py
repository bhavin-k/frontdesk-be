from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from recruitment.adapters.db.models import Base, Owned


class ScreeningRequest(Owned, Base):
    __tablename__ = "screening_requests"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "resume_id", "opening_id"),
        ForeignKeyConstraint(
            ["tenant_id", "application_id", "resume_id"],
            ["resumes.tenant_id", "resumes.application_id", "resumes.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "opening_id"],
            ["job_openings.tenant_id", "job_openings.id"],
        ),
        Index("ix_screening_queue", "tenant_id", text("created_at DESC"), "id"),
        Index("ix_screening_application", "tenant_id", "application_id"),
    )

    application_id: Mapped[UUID]
    resume_id: Mapped[UUID]
    opening_id: Mapped[UUID]
    candidate_name: Mapped[str]
    candidate_email: Mapped[str]
    callback_number: Mapped[str]
    role_title: Mapped[str]
    candidate_timezone: Mapped[str | None]
    status: Mapped[str]
    reason: Mapped[str]
    generation: Mapped[int] = mapped_column(default=1)
    lock_version: Mapped[int] = mapped_column(default=1)
    auto_replies: Mapped[int] = mapped_column(default=0)
    takeover: Mapped[bool] = mapped_column(default=False)
    first_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pending_slot_id: Mapped[UUID | None]


class ScreeningThread(Owned, Base):
    __tablename__ = "screening_threads"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "request_id"),
        UniqueConstraint("tenant_id", "id", "mailbox"),
        ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["screening_requests.tenant_id", "screening_requests.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "mailbox_connection_id"],
            ["mailbox_connections.tenant_id", "mailbox_connections.id"],
            name="fk_screening_thread_mailbox",
        ),
        Index("ix_screening_thread_mailbox", "tenant_id", "mailbox_connection_id"),
    )

    request_id: Mapped[UUID]
    mailbox_connection_id: Mapped[UUID | None]
    mailbox: Mapped[str]
    subject: Mapped[str]


class ScreeningMessage(Owned, Base):
    __tablename__ = "screening_messages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "mailbox", "message_id"),
        ForeignKeyConstraint(
            ["tenant_id", "thread_id", "mailbox"],
            ["screening_threads.tenant_id", "screening_threads.id", "screening_threads.mailbox"],
            ondelete="CASCADE",
        ),
        Index("ix_screening_thread_messages", "tenant_id", "thread_id", "created_at", "id"),
        Index(
            "ix_screening_send_rate",
            "tenant_id",
            "last_attempt_at",
            postgresql_where=text("direction='out' AND last_attempt_at IS NOT NULL"),
        ),
    )

    thread_id: Mapped[UUID]
    mailbox: Mapped[str]
    message_id: Mapped[str]
    in_reply_to: Mapped[str | None]
    references: Mapped[list[str]] = mapped_column("reply_references", JSON, default=list)
    offered_slot_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    generation: Mapped[int]
    direction: Mapped[str]
    kind: Mapped[str]
    state: Mapped[str]
    body: Mapped[str]
    intent: Mapped[str | None]
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScreeningSlot(Owned, Base):
    __tablename__ = "screening_slots"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["screening_requests.tenant_id", "screening_requests.id"],
            ondelete="CASCADE",
        ),
        Index(
            "uq_screening_occupied_start",
            "tenant_id",
            "starts_at",
            unique=True,
            postgresql_where=text("status IN ('held','booked')"),
        ),
        Index(
            "uq_screening_request_booking",
            "tenant_id",
            "request_id",
            unique=True,
            postgresql_where=text("status='booked'"),
        ),
        Index("ix_screening_request_slots", "tenant_id", "request_id"),
    )

    request_id: Mapped[UUID]
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    blocked_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str]


class ScreeningSettings(Owned, Base):
    __tablename__ = "screening_settings"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    invitation_note: Mapped[str] = mapped_column(default="")
    signature: Mapped[str] = mapped_column(default="")


class ScreeningInbox(Owned, Base):
    __tablename__ = "screening_inboxes"
    __table_args__ = (UniqueConstraint("tenant_id", "mailbox"),)

    mailbox: Mapped[str]
    cursor: Mapped[dict[str, str | int]] = mapped_column(JSON)


class ScreeningEvent(Owned, Base):
    __tablename__ = "screening_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "request_id"],
            ["screening_requests.tenant_id", "screening_requests.id"],
            ondelete="CASCADE",
        ),
        Index("ix_screening_events", "tenant_id", "request_id", "created_at"),
    )

    request_id: Mapped[UUID]
    status: Mapped[str]
    actor: Mapped[str]
    actor_id: Mapped[UUID | None]
    reason: Mapped[str]
