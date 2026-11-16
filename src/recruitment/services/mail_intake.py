"""Fenced mailbox jobs. Source cursors advance only with durable intake/outbox records."""

import logging
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from recruitment.adapters.db.models import Application, CVUpload, Opening, RetentionSetting
from recruitment.adapters.db.phase2 import (
    IdentityReview,
    MailAttachment,
    Mailbox,
    MailboxCredential,
    MailMessage,
    OAuthAttempt,
)
from recruitment.adapters.documents import DOC, DOCX, MAX_BYTES, PDF, validate_file
from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.adapters.providers import Providers
from recruitment.core.errors import AppError, ProviderError
from recruitment.core.time import utcnow
from recruitment.ports.mailbox import CursorExpired, MailboxProvider, MailboxReader, RemoteMessage
from recruitment.ports.queue import Lease, LeaseLost, WorkerQueue
from recruitment.services.common import emit
from recruitment.services.intake import store_cv
from recruitment.services.mailboxes import digest, read_credentials, save_credentials
from recruitment.services.openings import intake_allowed

logger = logging.getLogger(__name__)


def attachment_mime(filename: str, declared: str) -> str | None:
    extension = filename.casefold().rsplit(".", 1)[-1]
    expected = {"pdf": PDF, "docx": DOCX, "doc": DOC}.get(extension)
    if not expected:
        return None
    return expected if declared in {expected, "application/octet-stream", ""} else None


@dataclass
class ConnectionWork:
    id: UUID
    generation: int
    source_period: int
    folder: str
    provider: MailboxReader
    connection_method: str
    credentials: dict[str, Any]
    cursor: dict[str, Any]
    after: datetime
    scan_started: datetime


class MailboxJobs:
    def __init__(self, queue: WorkerQueue, providers: Providers, runtime: MailboxRuntime) -> None:
        self.queue, self.providers, self.runtime = queue, providers, runtime

    def _begin(self, lease: Lease, connection: UUID) -> ConnectionWork | None:
        with self.queue.owned(lease) as session:
            row = session.scalar(select(Mailbox).where(Mailbox.id == connection).with_for_update())
            if (
                not row
                or row.status != "connected"
                or not self.runtime.settings.mailbox_method_enabled(row.connection_method)
            ):
                return None
            if (
                row.lease_token
                and row.lease_token != lease.lease_token
                and row.lease_expires_at
                and row.lease_expires_at > utcnow()
            ):
                raise ProviderError("MAILBOX_BUSY", retryable=True)
            row.lease_token, row.lease_expires_at = (
                lease.lease_token,
                utcnow() + timedelta(seconds=self.queue.settings.lease_seconds),
            )
            if lease.kind == "mailbox.sync":
                row.sync_status = "running"
            assert row.intake_started_at
            cursor = dict(row.cursor)
            after = max(row.intake_started_at, row.completed_watermark or row.intake_started_at)
            if cursor.get("_recovery_after"):
                after = max(after, datetime.fromisoformat(cursor["_recovery_after"]))
            started = (
                datetime.fromisoformat(cursor["_scan_started"])
                if cursor.get("_scan_started")
                else utcnow()
            )
            return ConnectionWork(
                row.id,
                row.generation,
                row.source_period,
                row.folder_id,
                self.runtime.reader(row.provider, row.connection_method),
                row.connection_method,
                read_credentials(session, self.runtime, row),
                cursor,
                after,
                started,
            )

    @contextmanager
    def _owned(self, lease: Lease, work: ConnectionWork) -> Iterator[tuple[Session, Mailbox]]:
        with self.queue.owned(lease) as session:
            row = session.scalar(select(Mailbox).where(Mailbox.id == work.id).with_for_update())
            if (
                not row
                or row.status != "connected"
                or not self.runtime.settings.mailbox_method_enabled(row.connection_method)
                or row.generation != work.generation
                or row.lease_token != lease.lease_token
                or not row.lease_expires_at
                or row.lease_expires_at <= utcnow()
            ):
                raise LeaseLost
            row.lease_expires_at = utcnow() + timedelta(seconds=self.queue.settings.lease_seconds)
            yield session, row

    def _release(self, lease: Lease, work: ConnectionWork) -> None:
        with suppress(LeaseLost), self._owned(lease, work) as (session, row):
            save_credentials(session, self.runtime, row, work.credentials)
            row.lease_token = row.lease_expires_at = None

    def sync(self, lease: Lease) -> None:
        work = self._begin(lease, lease.aggregate_id)
        if not work:
            return
        try:
            if not work.cursor:
                work.cursor = work.provider.baseline(work.credentials, work.folder)
            for _ in range(10):
                try:
                    page = work.provider.page(
                        work.credentials, work.folder, work.cursor, work.after
                    )
                except CursorExpired:
                    work.after = max(work.after, utcnow() - timedelta(days=7))
                    work.cursor = work.provider.baseline(work.credentials, work.folder)
                    work.cursor["_recovery_after"] = work.after.isoformat()
                    with self._owned(lease, work) as (session, row):
                        row.cursor = {**work.cursor, "_scan_started": work.scan_started.isoformat()}
                        save_credentials(session, self.runtime, row, work.credentials)
                    continue
                with self._owned(lease, work) as (session, row):
                    for message in page.messages:
                        # Defense in depth: providers may return older bootstrap metadata.
                        if work.after <= message.received_at <= utcnow() + timedelta(minutes=5):
                            self._message(session, lease, row, message)
                    cursor = dict(page.cursor)
                    if page.complete:
                        row.completed_watermark = max(
                            row.completed_watermark or work.scan_started, work.scan_started
                        )
                        row.last_success_at, row.failure_code, row.sync_status = (
                            utcnow(),
                            None,
                            "idle",
                        )
                        cursor.pop("_scan_started", None)
                        cursor.pop("_recovery_after", None)
                    else:
                        cursor["_scan_started"] = work.scan_started.isoformat()
                        if work.cursor.get("_recovery_after"):
                            cursor["_recovery_after"] = work.cursor["_recovery_after"]
                    row.cursor = cursor
                    save_credentials(session, self.runtime, row, work.credentials)
                work.cursor = cursor
                if page.complete:
                    return
            with self._owned(lease, work) as (session, row):
                row.sync_status = "queued"
                emit(session, lease.tenant_id, "mailbox.sync", row.id)
        except ProviderError:
            # A provider failure from a superseded configuration cannot change its successor.
            with self._owned(lease, work):
                pass
            raise
        finally:
            self._release(lease, work)

    def _message(
        self, session: Session, lease: Lease, row: Mailbox, message: RemoteMessage
    ) -> None:
        if (message.sender and message.sender.casefold() == (row.email or "").casefold()) or (
            message.automated and not message.bounce
        ):
            return
        if session.scalar(
            select(MailMessage.id).where(
                MailMessage.connection_id == row.id, MailMessage.provider_message_id == message.id
            )
        ):
            return
        supported = [
            (item, mime)
            for item in message.attachments
            if not message.bounce and (mime := attachment_mime(item.filename, item.mime_type))
        ]
        if supported:
            opening = session.scalar(
                select(Opening).where(Opening.id == row.opening_id).with_for_update()
            )
            if not opening or not intake_allowed(session, opening, message.received_at):
                return
        related: UUID | None = None
        if not supported and (message.in_reply_to or message.thread_id):
            references = []
            if message.in_reply_to:
                references.append(MailMessage.internet_message_id == message.in_reply_to)
            if message.thread_id:
                references.append(MailMessage.provider_thread_id == message.thread_id)
            # A thread can contain CVs for several people; ambiguous replies need no automatic link.
            candidates = list(
                session.scalars(
                    select(Application.candidate_id)
                    .distinct()
                    .join(CVUpload, CVUpload.application_id == Application.id)
                    .join(MailAttachment, MailAttachment.upload_id == CVUpload.id)
                    .join(MailMessage, MailMessage.id == MailAttachment.message_id)
                    .where(MailMessage.connection_id == row.id, or_(*references))
                    .limit(2)
                )
            )
            related = candidates[0] if len(candidates) == 1 else None
        if not supported and not related:
            return
        source = MailMessage(
            id=uuid4(),
            tenant_id=lease.tenant_id,
            connection_id=row.id,
            provider_message_id=message.id,
            internet_message_id=message.internet_message_id,
            provider_thread_id=message.thread_id,
            received_at=message.received_at,
            generation=row.source_period,
        )
        session.add(source)
        session.flush()
        for item, mime in supported[:100]:
            attachment = MailAttachment(
                id=uuid4(),
                tenant_id=lease.tenant_id,
                message_id=source.id,
                provider_attachment_id=item.id,
                filename=item.filename.replace("\\", "/").rsplit("/", 1)[-1][:255],
                mime_type=mime,
                byte_length=max(0, item.size),
                status="needs_review" if item.size > MAX_BYTES else "queued",
                failure_code="FILE_SIZE" if item.size > MAX_BYTES else None,
            )
            session.add(attachment)
            session.flush()
            if item.size <= MAX_BYTES:
                emit(
                    session,
                    lease.tenant_id,
                    "mail.attachment",
                    attachment.id,
                    f"mail.attachment:{attachment.id}",
                )
        if related:
            # Proposal only. The sender/connected mailbox is deliberately absent.
            emails = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", message.reply_text)
            phones = re.findall(r"\+?[0-9][0-9 ()-]{8,18}[0-9]", message.reply_text)
            proposed = {"email": emails[0]} if len(set(emails)) == 1 else {}
            if len(set(phones)) == 1:
                proposed["mobile"] = phones[0]
            session.add(
                IdentityReview(
                    tenant_id=lease.tenant_id,
                    message_id=source.id,
                    candidate_id=related,
                    kind="contact_issue" if message.bounce else "contact_proposal",
                    proposed_contacts=proposed,
                    candidate_options=[str(related)],
                )
            )

    def attachment(self, lease: Lease) -> None:
        with self.queue.owned(lease) as session:
            attachment = session.get(MailAttachment, lease.aggregate_id)
            if not attachment or attachment.status == "completed":
                return
            message = session.get(MailMessage, attachment.message_id)
            if not message:
                return
            connection, generation, received = (
                message.connection_id,
                message.generation,
                message.received_at,
            )
            remote_message, remote_attachment, mime = (
                message.provider_message_id,
                attachment.provider_attachment_id,
                attachment.mime_type,
            )
        work = self._begin(lease, connection)
        if not work:
            raise ProviderError("MAILBOX_DISCONNECTED")
        try:
            if generation != work.source_period:
                raise ProviderError("MAILBOX_CONFIGURATION_CHANGED")
            with self._owned(lease, work) as (session, row):
                item = session.get(MailAttachment, lease.aggregate_id)
                if not item:
                    return
                item.status = "processing"
            content = work.provider.attachment(work.credentials, remote_message, remote_attachment)
            validate_file(content, mime)
            self.providers.scanner.scan(content)
            with self._owned(lease, work) as (session, row):
                item = session.get(MailAttachment, lease.aggregate_id)
                if not item:
                    return
                policy = session.scalar(select(RetentionSetting))
                assert policy is not None and row.intake_started_at
                if received < row.intake_started_at or received < utcnow() - timedelta(
                    days=policy.other_candidate_retention_days
                ):
                    raise ProviderError("INTAKE_EXPIRED")
                upload, _ = store_cv(
                    session,
                    lease.tenant_id,
                    row.opening_id,
                    content,
                    mime,
                    received,
                    scoring_strategy=self.queue.settings.scoring_strategy,
                )
                item.upload_id, item.status, item.failure_code = upload.id, "completed", None
                save_credentials(session, self.runtime, row, work.credentials)
        except AppError as exc:
            raise ProviderError(exc.code) from exc
        finally:
            self._release(lease, work)

    def maintain(self, lease: Lease) -> None:
        work = self._begin(lease, lease.aggregate_id)
        if not work:
            return
        try:
            if work.connection_method == "imap":
                return
            with self._owned(lease, work) as (session, row):
                raw_secret = work.credentials.get("notification_secret")
                if not raw_secret:
                    raw_secret = secrets.token_urlsafe(32)
                    work.credentials["notification_secret"] = raw_secret
                    # A new authorization may replace the cache; create a fresh subscription.
                    if row.subscription_id:
                        work.credentials.setdefault("retired_subscriptions", []).append(
                            row.subscription_id
                        )
                    row.subscription_id = None
                row.notification_secret_hash = digest(raw_secret)
                previous, expiry = row.subscription_id, row.subscription_expires_at
                last_success = row.last_success_at or row.intake_started_at
                save_credentials(session, self.runtime, row, work.credentials)
                if last_success and last_success < utcnow() - timedelta(hours=1):
                    emit(
                        session,
                        lease.tenant_id,
                        "mailbox.stalled",
                        row.id,
                        f"mailbox.stalled:{row.id}:{utcnow().strftime('%Y%m%d%H')}",
                    )
                    logger.warning("mailbox.stalled connection_id=%s", row.id)
            for old in list(work.credentials.get("retired_subscriptions", [])):
                work.provider.disconnect(work.credentials, old)
                work.credentials["retired_subscriptions"].remove(old)
            # Gmail gets a daily watch; Graph renews before its remaining 48-hour window.
            renew = not expiry or expiry <= utcnow() + timedelta(hours=48)
            if work.provider.name == "gmail" and expiry:
                renew = expiry <= utcnow() + timedelta(days=6)
            if renew:
                base = (
                    self.runtime.settings.public_webhook_url or self.runtime.settings.public_app_url
                )
                callback = f"{base.rstrip('/')}/api/v1/mailbox-webhooks/microsoft/{work.id}"
                subscription = cast(MailboxProvider, work.provider).subscribe(
                    work.credentials, work.folder, callback, raw_secret, previous
                )
                with self._owned(lease, work) as (session, row):
                    row.subscription_id, row.subscription_expires_at = (
                        subscription.id,
                        subscription.expires_at,
                    )
                    save_credentials(session, self.runtime, row, work.credentials)
        except ProviderError:
            with self._owned(lease, work):
                pass
            raise
        finally:
            self._release(lease, work)

    def disconnect(self, lease: Lease) -> None:
        with self.queue.owned(lease) as session:
            row = session.get(Mailbox, lease.aggregate_id)
            if not row or row.status != "disconnecting":
                return
            provider, previous, generation = (
                self.runtime.reader(row.provider, row.connection_method),
                row.subscription_id,
                row.generation,
            )
            try:
                credentials = read_credentials(session, self.runtime, row)
            except (AppError, ProviderError):
                credentials = None
        if credentials is not None:
            try:
                provider.disconnect(credentials, previous)
            except ProviderError as exc:
                if exc.retryable and lease.attempts < self.queue.settings.max_job_attempts:
                    raise
        with self.queue.owned(lease) as session:
            row = session.get(Mailbox, lease.aggregate_id)
            if not row or row.generation != generation or row.status != "disconnecting":
                return
            session.execute(
                delete(MailboxCredential).where(MailboxCredential.connection_id == row.id)
            )
            session.execute(delete(OAuthAttempt).where(OAuthAttempt.connection_id == row.id))
            row.status, row.sync_status = "disconnected", "idle"
            row.cursor = {}
            row.email = row.owner_hash = row.address_hash = row.notification_lookup = (
                row.notification_secret_hash
            ) = row.subscription_id = None
            row.lease_token = row.lease_expires_at = row.subscription_expires_at = None
