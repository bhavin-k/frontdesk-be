import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from pwdlib import PasswordHash
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from recruitment.adapters.db.better_auth import BetterAuthSession
from recruitment.adapters.db.models import AuthSession, LoginAttempt, User
from recruitment.adapters.db.session import Database
from recruitment.core.config import Settings
from recruitment.core.errors import AppError
from recruitment.core.time import utcnow
from recruitment.services.common import advisory_lock


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    session_id: UUID


class AuthService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings
        self.hasher = PasswordHash.recommended()
        self.dummy_hash = self.hasher.hash(secrets.token_urlsafe(32))

    def register(self, email: str, password: str) -> UUID:
        try:
            with self.db.transaction(self.db.auth) as session:
                user = User(
                    email=email.strip().casefold(), password_hash=self.hasher.hash(password)
                )
                session.add(user)
                session.flush()
                return user.id
        except IntegrityError as exc:
            raise AppError(
                "ACCOUNT_CONFLICT", "An account cannot be created with these details.", 409
            ) from exc

    def login(self, email: str, password: str) -> tuple[str, int]:
        identity = hashlib.sha256(email.strip().casefold().encode()).hexdigest()
        error: AppError | None = None
        token = ""
        with self.db.transaction(self.db.auth) as session:
            advisory_lock(session, f"login:{identity}")
            now = utcnow()
            session.execute(
                delete(LoginAttempt).where(LoginAttempt.created_at < now - timedelta(days=1))
            )
            count = (
                session.scalar(
                    select(func.count())
                    .select_from(LoginAttempt)
                    .where(
                        LoginAttempt.identity_hash == identity,
                        LoginAttempt.created_at > now - timedelta(minutes=15),
                    )
                )
                or 0
            )
            if count >= self.settings.login_attempt_limit:
                error = AppError("RATE_LIMITED", "Try signing in again later.", 429)
            else:
                session.add(LoginAttempt(identity_hash=identity))
                user = session.scalar(select(User).where(User.email == email.strip().casefold()))
                valid = self.hasher.verify(
                    password, user.password_hash if user and user.password_hash else self.dummy_hash
                )
                if not user or not user.password_hash or not valid:
                    error = AppError("INVALID_CREDENTIALS", "Email or password is incorrect.", 401)
                else:
                    # Serialize session creation for this account and bound outstanding sessions.
                    advisory_lock(session, f"session:{user.id}")
                    active = session.scalars(
                        select(AuthSession)
                        .where(
                            AuthSession.user_id == user.id,
                            AuthSession.revoked_at.is_(None),
                            AuthSession.expires_at > now,
                        )
                        .order_by(AuthSession.created_at)
                    ).all()
                    for old in active[: max(0, len(active) - self.settings.session_limit + 1)]:
                        old.revoked_at = now
                    token = secrets.token_urlsafe(48)
                    session.add(
                        AuthSession(
                            user_id=user.id,
                            token_hash=hashlib.sha256(token.encode()).hexdigest(),
                            expires_at=now + timedelta(seconds=self.settings.session_seconds),
                        )
                    )
        if error:
            raise error
        return token, self.settings.session_seconds

    def authenticate(self, token: str) -> Principal:
        if not 32 <= len(token) <= 256:
            raise AppError("UNAUTHENTICATED", "Sign in to continue.", 401)
        with self.db.transaction(self.db.auth) as session:
            web_session = session.scalar(
                select(BetterAuthSession).where(
                    BetterAuthSession.token == token,
                    BetterAuthSession.expires_at > utcnow(),
                )
            )
            if web_session:
                return Principal(web_session.user_id, web_session.id)
            record = session.scalar(
                select(AuthSession).where(
                    AuthSession.token_hash == hashlib.sha256(token.encode()).hexdigest(),
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > utcnow(),
                )
            )
            if not record:
                raise AppError("UNAUTHENTICATED", "Sign in to continue.", 401)
            return Principal(record.user_id, record.id)

    def logout(self, principal: Principal) -> None:
        with self.db.transaction(self.db.auth) as session:
            session.execute(
                delete(BetterAuthSession).where(
                    BetterAuthSession.id == principal.session_id,
                    BetterAuthSession.user_id == principal.user_id,
                )
            )
            session.execute(
                update(AuthSession)
                .where(
                    AuthSession.id == principal.session_id, AuthSession.user_id == principal.user_id
                )
                .values(revoked_at=utcnow())
            )
