import base64
import hmac
import json
from typing import Any
from uuid import UUID

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token
from sqlalchemy import select, text

from recruitment.adapters.db.phase2 import Mailbox, MailboxReceipt
from recruitment.adapters.db.session import Database
from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.core.errors import AppError
from recruitment.services.common import emit
from recruitment.services.mailboxes import digest


def verify_google_push(runtime: MailboxRuntime, authorization: str) -> None:
    settings = runtime.settings
    if (
        settings.mailbox_provider_mode != "real"
        or not settings.gmail_push_audience
        or not settings.gmail_push_service_account
        or not authorization.startswith("Bearer ")
        or len(authorization) > 8192
    ):
        raise AppError("INVALID_NOTIFICATION", "Notification authentication failed.", 401)
    try:
        claims = id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
            authorization.removeprefix("Bearer "),
            GoogleRequest(),
            audience=settings.gmail_push_audience,
        )
        if (
            claims.get("email") != settings.gmail_push_service_account
            or claims.get("email_verified") is not True
            or claims.get("iss") not in {"https://accounts.google.com", "accounts.google.com"}
        ):
            raise ValueError
    except Exception as exc:
        raise AppError("INVALID_NOTIFICATION", "Notification authentication failed.", 401) from exc


class MailNotifications:
    def __init__(self, db: Database, runtime: MailboxRuntime) -> None:
        self.db, self.runtime = db, runtime

    def _target(self, provider: str, lookup: str) -> dict[str, Any] | None:
        with self.db.transaction(self.db.dispatcher) as session:
            row = (
                session.execute(
                    text("SELECT * FROM mail_notification_target(:provider,:lookup)"),
                    {"provider": provider, "lookup": lookup},
                )
                .mappings()
                .first()
            )
            return dict(row) if row else None

    def _queue(self, target: dict[str, Any], receipt: str) -> None:
        with self.db.transaction(self.db.worker, target["tenant_id"]) as session:
            row = session.scalar(
                select(Mailbox).where(Mailbox.id == target["id"]).with_for_update()
            )
            if (
                not row
                or row.status != "connected"
                or not self.runtime.settings.mailbox_method_enabled(row.connection_method)
            ):
                return
            hashed = digest(receipt)
            if session.scalar(
                select(MailboxReceipt.id).where(
                    MailboxReceipt.connection_id == row.id, MailboxReceipt.receipt_hash == hashed
                )
            ):
                return
            session.add(
                MailboxReceipt(tenant_id=row.tenant_id, connection_id=row.id, receipt_hash=hashed)
            )
            if row.sync_status != "running":
                row.sync_status = "queued"
            emit(session, row.tenant_id, "mailbox.sync", row.id)

    def gmail(self, payload: dict[str, Any], authorization: str) -> None:
        verify_google_push(self.runtime, authorization)
        try:
            envelope = payload["message"]
            encoded = str(envelope["data"])
            if len(encoded) > 16384 or len(str(envelope["messageId"])) > 512:
                raise ValueError
            data = json.loads(base64.b64decode(encoded, validate=True))
            email, receipt = str(data["emailAddress"]).casefold(), str(envelope["messageId"])
        except (ValueError, TypeError, KeyError) as exc:
            raise AppError("INVALID_NOTIFICATION", "Invalid notification envelope.", 422) from exc
        target = self._target("gmail", digest(email))
        if target:
            self._queue(target, receipt)

    def microsoft(self, connection: UUID, payload: dict[str, Any]) -> None:
        notices = payload.get("value", [])
        if not isinstance(notices, list) or not 1 <= len(notices) <= 100:
            raise AppError("INVALID_NOTIFICATION", "Invalid notification envelope.", 422)
        target = self._target("microsoft", str(connection))
        if not target:
            raise AppError("INVALID_NOTIFICATION", "Notification authentication failed.", 401)
        receipts = []
        for notice in notices:
            if not isinstance(notice, dict) or not isinstance(notice.get("resourceData", {}), dict):
                raise AppError("INVALID_NOTIFICATION", "Invalid notification envelope.", 422)
            secret = notice.get("clientState", "")
            subscription = notice.get("subscriptionId", "")
            if (
                not isinstance(secret, str)
                or len(secret) > 512
                or not target["notification_secret_hash"]
                or not hmac.compare_digest(digest(secret), target["notification_secret_hash"])
                or (target["subscription_id"] and subscription != target["subscription_id"])
            ):
                raise AppError("INVALID_NOTIFICATION", "Notification authentication failed.", 401)
            receipts.append(
                json.dumps(
                    {
                        "subscription": subscription,
                        "change": notice.get("changeType"),
                        "resource": notice.get("resource"),
                        "id": notice.get("resourceData", {}).get("id"),
                    },
                    sort_keys=True,
                )
            )
        for receipt in receipts:
            self._queue(target, receipt)
