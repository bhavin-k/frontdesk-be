from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKeyConstraint,
    Index,
    LargeBinary,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from recruitment.adapters.db.models import Base, Owned


class OpeningDocument(Owned, Base):
    __tablename__ = "opening_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("opening_id", "sha256"),
        ForeignKeyConstraint(
            ["tenant_id", "opening_id"],
            ["job_openings.tenant_id", "job_openings.id"],
            ondelete="CASCADE",
        ),
    )
    opening_id: Mapped[UUID]
    file_name: Mapped[str]
    mime_type: Mapped[str]
    byte_length: Mapped[int]
    sha256: Mapped[str]
    content: Mapped[bytes] = mapped_column(LargeBinary)


class OpeningIntakePeriod(Owned, Base):
    __tablename__ = "opening_intake_periods"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "opening_id"],
            ["job_openings.tenant_id", "job_openings.id"],
            ondelete="CASCADE",
        ),
        Index("ix_opening_intake_received", "opening_id", "starts_at", "ends_at"),
        Index(
            "uq_opening_intake_current",
            "opening_id",
            unique=True,
            postgresql_where=text("ends_at IS NULL"),
        ),
    )
    opening_id: Mapped[UUID]
    match_description: Mapped[dict[str, Any]] = mapped_column(JSON)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
