"""Recruiter-authorized mailbox connections; provider grants remain server-side."""

import hashlib
import secrets
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import Opening
from recruitment.adapters.db.phase2 import Mailbox, MailboxCredential, OAuthAttempt
from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.core.errors import AppError
from recruitment.core.time import ist_display, utcnow
from recruitment.ports.mailbox import Sealed
from recruitment.services.auth import Principal
from recruitment.services.common import advisory_lock, emit, missing
from recruitment.services.tenancy import TenancyService


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def mailbox_address_hash(email: str) -> str:
    address = email.strip().casefold()
    local, domain = address.rsplit("@", 1)
    if domain in {"gmail.com", "googlemail.com"}:
        address = local.split("+", 1)[0].replace(".", "") + "@gmail.com"
    return digest(address)


def credential_context(tenant: UUID, connection: UUID) -> str:
    return f"{tenant}:{connection}:mailbox"


def read_credentials(session: Session, runtime: MailboxRuntime, mailbox: Mailbox) -> dict[str, Any]:
    row = session.scalar(
        select(MailboxCredential).where(MailboxCredential.connection_id == mailbox.id)
    )
    if not row:
        raise AppError("MAILBOX_REAUTHORIZATION_REQUIRED", "Reconnect the mailbox.", 409)
    return runtime.vault.open(
        Sealed(row.key_id, row.nonce, row.ciphertext),
        credential_context(mailbox.tenant_id, mailbox.id),
    )


def save_credentials(
    session: Session, runtime: MailboxRuntime, mailbox: Mailbox, value: dict[str, Any]
) -> None:
    sealed = runtime.vault.seal(value, credential_context(mailbox.tenant_id, mailbox.id))
    row = session.scalar(
        select(MailboxCredential).where(MailboxCredential.connection_id == mailbox.id)
    )
    if not row:
        row = MailboxCredential(tenant_id=mailbox.tenant_id, connection_id=mailbox.id)
        session.add(row)
    row.key_id, row.nonce, row.ciphertext = sealed.key_id, sealed.nonce, sealed.ciphertext


def mailbox_read(row: Mailbox, runtime: MailboxRuntime) -> dict[str, Any]:
    reference = row.last_success_at or row.intake_started_at
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "provider": row.provider,
        "connection_method": row.connection_method,
        "enabled": runtime.settings.mailbox_method_enabled(row.connection_method),
        "email": row.email,
        "status": row.status,
        "opening_id": row.opening_id,
        "folder_id": row.folder_id,
        "generation": row.generation,
        "intake_started_at": row.intake_started_at,
        "intake_started_display_ist": ist_display(row.intake_started_at)
        if row.intake_started_at
        else None,
        "sync_status": row.sync_status,
        "last_success_at": row.last_success_at,
        "failure_code": row.failure_code,
        "stalled": bool(
            runtime.settings.mailbox_method_enabled(row.connection_method)
            and row.status == "connected"
            and reference
            and reference < utcnow() - timedelta(hours=1)
        ),
        "provider_mode": runtime.settings.mailbox_provider_mode,
    }


class MailboxService:
    def __init__(self, tenants: TenancyService, runtime: MailboxRuntime) -> None:
        self.tenants, self.runtime = tenants, runtime

    def capabilities(self, actor: Principal, tenant: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant):
            return {
                "provider_mode": self.runtime.settings.mailbox_provider_mode,
                "items": [
                    {
                        "id": name,
                        "connection_method": method,
                        "configured": self.runtime.method_configured(method),
                        "mailbox_type": "signed_in_owner",
                    }
                    for name, method in (
                        ("gmail", "imap"),
                        ("gmail", "gmail_oauth"),
                        ("microsoft", "microsoft_oauth"),
                    )
                    if self.runtime.settings.mailbox_method_enabled(method)
                ],
            }

    def _authorize(self, session: Session, actor: Principal, row: Mailbox) -> dict[str, Any]:
        self.runtime.ensure_enabled(row.connection_method)
        if row.connection_method == "imap":
            raise AppError(
                "IMAP_CREDENTIALS_REQUIRED", "Enter a new App Password to reconnect.", 422
            )
        if not self.runtime.configured(row.provider):
            raise AppError(
                "MAILBOX_NOT_CONFIGURED", "Configure this provider before connecting.", 503
            )
        state = secrets.token_urlsafe(32)
        base = self.runtime.settings.public_app_url.rstrip("/")
        redirect = f"{base}/api/recruitment/oauth/{row.provider}/callback"
        start = self.runtime.providers[row.provider].authorize(state, redirect)
        attempt_id = uuid4()
        flow = {**start.flow, "_generation": row.generation, "_redirect": redirect}
        if self.runtime.settings.mailbox_provider_mode == "fake":
            flow["_fake_recruiter_id"] = str(actor.user_id)
        sealed = self.runtime.vault.seal(flow, f"{row.tenant_id}:{attempt_id}:oauth")
        session.add(
            OAuthAttempt(
                id=attempt_id,
                tenant_id=row.tenant_id,
                connection_id=row.id,
                user_id=actor.user_id,
                state_hash=digest(state),
                expires_at=utcnow() + timedelta(minutes=10),
                key_id=sealed.key_id,
                nonce=sealed.nonce,
                ciphertext=sealed.ciphertext,
            )
        )
        return {**mailbox_read(row, self.runtime), "authorization_url": start.url}

    def connect(
        self, actor: Principal, tenant: UUID, provider: str, opening: UUID
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            role = session.get(Opening, opening)
            if not role or role.status != "active":
                raise missing()
            row = Mailbox(
                id=uuid4(),
                tenant_id=tenant,
                provider=provider,
                connection_method=f"{provider}_oauth",
                recruiter_id=actor.user_id,
                opening_id=opening,
                folder_id="INBOX" if provider == "gmail" else "inbox",
            )
            session.add(row)
            session.flush()
            return self._authorize(session, actor, row)

    def connect_imap(
        self, actor: Principal, tenant: UUID, opening: UUID, email: str, password: str
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            role = session.get(Opening, opening)
            if not role or role.status != "active":
                raise missing()
        self.runtime.ensure_enabled("imap")
        if not self.runtime.method_configured("imap"):
            raise AppError(
                "MAILBOX_NOT_CONFIGURED",
                "Configure encrypted mailbox credentials before connecting.",
                503,
            )
        grant = self.runtime.imap.login(email, password)
        with self.tenants.scope(actor, tenant) as session:
            role = session.get(Opening, opening)
            if not role or role.status != "active":
                raise missing()
            row = Mailbox(
                id=uuid4(),
                tenant_id=tenant,
                provider="gmail",
                connection_method="imap",
                recruiter_id=actor.user_id,
                opening_id=opening,
                folder_id="INBOX",
                status="needs_configuration",
                email=grant.email,
                owner_hash=digest(f"imap:{grant.owner_id}"),
                address_hash=mailbox_address_hash(grant.email),
            )
            session.add(row)
            session.flush()
            save_credentials(session, self.runtime, row, grant.credentials)
            return mailbox_read(row, self.runtime)

    def refresh_imap(
        self, actor: Principal, tenant: UUID, connection: UUID, generation: int, password: str
    ) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = session.get(Mailbox, connection)
            if not row or row.connection_method != "imap":
                raise missing()
            self.runtime.ensure_enabled("imap")
            if row.generation != generation or row.status in {"disconnecting", "disconnected"}:
                raise AppError("STATE_CONFLICT", "Refresh the mailbox settings.", 409)
            assert row.email
            email = row.email
        if not self.runtime.method_configured("imap"):
            raise AppError(
                "MAILBOX_NOT_CONFIGURED",
                "Configure encrypted mailbox credentials before reconnecting.",
                503,
            )
        grant = self.runtime.imap.login(email, password)
        with self.tenants.scope(actor, tenant) as session:
            row = session.scalar(select(Mailbox).where(Mailbox.id == connection).with_for_update())
            if (
                not row
                or row.generation != generation
                or row.status in {"disconnecting", "disconnected"}
            ):
                raise AppError("STATE_CONFLICT", "The connection changed. Start again.", 409)
            self.runtime.ensure_enabled("imap")
            save_credentials(session, self.runtime, row, grant.credentials)
            row.generation += 1
            row.lease_token = row.lease_expires_at = None
            row.failure_code = None
            row.status = "connected" if row.intake_started_at else "needs_configuration"
            if row.intake_started_at:
                row.next_sync_at, row.sync_status = utcnow(), "queued"
                emit(session, tenant, "mailbox.sync", row.id)
            return mailbox_read(row, self.runtime)

    def list(self, actor: Principal, tenant: UUID, limit: int, offset: int) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            rows = session.scalars(
                select(Mailbox)
                .order_by(Mailbox.created_at.desc(), Mailbox.id)
                .offset(offset)
                .limit(limit)
            )
            return {
                "items": [mailbox_read(row, self.runtime) for row in rows],
                "limit": limit,
                "offset": offset,
            }

    def get(self, actor: Principal, tenant: UUID, connection: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = session.get(Mailbox, connection)
            if not row:
                raise missing()
            return mailbox_read(row, self.runtime)

    def callback(
        self, actor: Principal, provider: str, state: str, code: str | None, error: str | None
    ) -> dict[str, Any]:
        with self.tenants.db.transaction(self.tenants.db.api, user_id=actor.user_id) as session:
            target = session.execute(
                text("SELECT * FROM mail_oauth_target(:state)"), {"state": digest(state)}
            ).first()
            if not target:
                raise AppError("OAUTH_EXPIRED", "Start mailbox authorization again.", 410)
            tenant, connection = target
        with self.tenants.scope(actor, tenant) as session:
            attempt = session.scalar(
                select(OAuthAttempt)
                .where(OAuthAttempt.state_hash == digest(state))
                .with_for_update()
            )
            row = session.get(Mailbox, connection)
            if (
                not attempt
                or attempt.consumed_at
                or attempt.expires_at <= utcnow()
                or not row
                or row.provider != provider
                or row.status in {"disconnected", "disconnecting"}
            ):
                raise AppError("OAUTH_EXPIRED", "Start mailbox authorization again.", 410)
            self.runtime.ensure_enabled(row.connection_method)
            flow = self.runtime.vault.open(
                Sealed(attempt.key_id, attempt.nonce, attempt.ciphertext),
                f"{tenant}:{attempt.id}:oauth",
            )
            if flow.pop("_generation") != row.generation:
                raise AppError("OAUTH_EXPIRED", "The connection changed. Start again.", 410)
            generation = row.generation
            attempt.consumed_at = utcnow()  # Commit before exchanging a single-use code.
        if error or not code:
            raise AppError("OAUTH_DECLINED", "Mailbox access was not granted.", 422)
        redirect = flow.pop("_redirect")
        grant = self.runtime.providers[provider].exchange(flow, code, redirect)
        with self.tenants.scope(actor, tenant) as session:
            advisory_lock(session, f"mailbox:{connection}")
            row = session.get(Mailbox, connection)
            if (
                not row
                or row.generation != generation
                or row.status in {"disconnected", "disconnecting"}
            ):
                raise AppError("STATE_CONFLICT", "The connection changed. Start again.", 409)
            self.runtime.ensure_enabled(row.connection_method)
            owner_hash = digest(f"{provider}:{grant.owner_id}")
            if row.owner_hash and row.owner_hash != owner_hash:
                raise AppError(
                    "MAILBOX_OWNER_CHANGED", "Reconnect the original mailbox account.", 409
                )
            row.email, row.owner_hash = grant.email, owner_hash
            row.address_hash = mailbox_address_hash(grant.email)
            row.notification_lookup = (
                digest(grant.email.casefold()) if provider == "gmail" else str(row.id)
            )
            save_credentials(session, self.runtime, row, grant.credentials)
            row.generation += 1
            row.failure_code = None
            row.status = "connected" if row.intake_started_at else "needs_configuration"
            if row.intake_started_at:
                row.next_sync_at = row.next_maintenance_at = utcnow()
                row.sync_status = "queued"
                emit(session, tenant, "mailbox.sync", row.id)
            return mailbox_read(row, self.runtime)

    def folders(self, actor: Principal, tenant: UUID, connection: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = session.get(Mailbox, connection)
            if not row or row.status in {"pending_authorization", "disconnecting", "disconnected"}:
                raise missing()
            self.runtime.ensure_enabled(row.connection_method)
            generation, provider, method = row.generation, row.provider, row.connection_method
            credentials = read_credentials(session, self.runtime, row)
        folders = self.runtime.reader(provider, method).folders(credentials)
        with self.tenants.scope(actor, tenant) as session:
            row = session.get(Mailbox, connection)
            if (
                row
                and row.generation == generation
                and row.status not in {"disconnecting", "disconnected"}
            ):
                save_credentials(session, self.runtime, row, credentials)
        return {"items": folders}

    def configure(
        self, actor: Principal, tenant: UUID, connection: UUID, values: dict[str, Any]
    ) -> dict[str, Any]:
        if not values.get("reauthorize"):
            folders = self.folders(actor, tenant, connection)["items"]
            available = {item["id"] for item in folders} | {"inbox", "INBOX"}
            if values.get("folder_id") and values["folder_id"] not in available:
                raise AppError("INVALID_FOLDER", "Choose a source folder from this mailbox.", 422)
        with self.tenants.scope(actor, tenant) as session:
            advisory_lock(session, f"mailbox:{connection}")
            row = session.scalar(select(Mailbox).where(Mailbox.id == connection).with_for_update())
            if not row:
                raise missing()
            self.runtime.ensure_enabled(row.connection_method)
            if row.generation != values["expected_generation"] or row.status in {
                "disconnected",
                "disconnecting",
            }:
                raise AppError("STATE_CONFLICT", "Refresh the mailbox settings.", 409)
            row.generation += 1
            row.lease_token = row.lease_expires_at = None
            if values.get("reauthorize"):
                row.status = "reauthorization_required"
                return self._authorize(session, actor, row)
            opening = session.get(Opening, values.get("opening_id") or row.opening_id)
            if not opening or opening.status != "active":
                raise missing()
            changed_folder = values.get("folder_id", row.folder_id) != row.folder_id
            changed_opening = opening.id != row.opening_id
            row.folder_id, row.opening_id = values.get("folder_id", row.folder_id), opening.id
            if changed_folder or changed_opening:
                credentials = read_credentials(session, self.runtime, row)
                if row.subscription_id:
                    credentials.setdefault("retired_subscriptions", []).append(row.subscription_id)
                    save_credentials(session, self.runtime, row, credentials)
                row.subscription_id = None
                row.source_period += 1
                row.cursor, row.completed_watermark = {}, None
                row.intake_started_at = utcnow()
                row.subscription_expires_at = None
            if values.get("activate") or row.status == "connected":
                row.intake_started_at = row.intake_started_at or utcnow()
                row.status, row.sync_status = "connected", "queued"
                row.next_sync_at = utcnow()
                emit(session, tenant, "mailbox.sync", row.id)
                if row.connection_method != "imap":
                    row.next_maintenance_at = utcnow()
                    emit(session, tenant, "mailbox.maintain", row.id)
            return mailbox_read(row, self.runtime)

    def sync(self, actor: Principal, tenant: UUID, connection: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            row = session.get(Mailbox, connection)
            if not row:
                raise missing()
            self.runtime.ensure_enabled(row.connection_method)
            if row.status != "connected":
                raise AppError(
                    "MAILBOX_NOT_CONNECTED", "Reconnect and configure the mailbox first.", 409
                )
            if row.sync_status not in {"queued", "running"}:
                row.sync_status = "queued"
                emit(session, tenant, "mailbox.sync", row.id)
            return mailbox_read(row, self.runtime)

    def disconnect(self, actor: Principal, tenant: UUID, connection: UUID) -> dict[str, Any]:
        with self.tenants.scope(actor, tenant) as session:
            advisory_lock(session, f"mailbox:{connection}")
            row = session.scalar(select(Mailbox).where(Mailbox.id == connection).with_for_update())
            if not row:
                raise missing()
            if row.status not in {"disconnecting", "disconnected"}:
                row.status = "disconnecting"
                row.generation += 1
                row.lease_token = row.lease_expires_at = None
                row.next_sync_at = row.next_maintenance_at = None
                emit(session, tenant, "mailbox.disconnect", row.id)
            return mailbox_read(row, self.runtime)
