"""Better Auth tables owned by the Next.js authentication service."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from recruitment.adapters.db.models import Base, Record
from recruitment.core.time import utcnow


class BetterAuthAccount(Record, Base):
    __tablename__ = "better_auth_accounts"
    __table_args__ = (
        UniqueConstraint("provider_id", "account_id"),
        Index("better_auth_accounts_user", "user_id"),
    )
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    account_id: Mapped[str] = mapped_column(Text)
    provider_id: Mapped[str] = mapped_column(Text)
    password: Mapped[str | None] = mapped_column(Text)
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    id_token: Mapped[str | None] = mapped_column(Text)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scope: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BetterAuthSession(Record, Base):
    __tablename__ = "better_auth_sessions"
    __table_args__ = (Index("better_auth_sessions_user", "user_id"),)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ip_address: Mapped[str | None] = mapped_column(Text)
    user_agent: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BetterAuthVerification(Record, Base):
    __tablename__ = "better_auth_verifications"
    __table_args__ = (Index("better_auth_verifications_identifier", "identifier"),)
    identifier: Mapped[str] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BetterAuthRateLimit(Base):
    __tablename__ = "better_auth_rate_limits"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(Text, unique=True)
    count: Mapped[int]
    last_request: Mapped[int] = mapped_column(BigInteger)
