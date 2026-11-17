"""Read-only Gmail Inbox adapter for development. No SMTP or mailbox writes."""

import imaplib
import re
import ssl
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from typing import Any

from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError
from recruitment.ports.mailbox import (
    CursorExpired,
    MailboxGrant,
    MessagePage,
    RemoteAttachment,
    RemoteMessage,
)

MAX_MESSAGE_BYTES = 32 * 1024 * 1024
PAGE_SIZE = 25


class GmailIMAPProvider:
    name = "gmail"

    def __init__(self, settings: Settings) -> None:
        self.timeout = settings.imap_timeout_seconds

    @contextmanager
    def _session(self, credentials: dict[str, Any]) -> Iterator[imaplib.IMAP4_SSL]:
        client = None
        try:
            # Fixed destination, certificate verification and bounded socket operations.
            client = imaplib.IMAP4_SSL(
                "imap.gmail.com",
                993,
                ssl_context=ssl.create_default_context(),
                timeout=self.timeout,
            )
            client.debug = 0
            try:
                client.login(credentials["email"], credentials["app_password"])
            except imaplib.IMAP4.abort:
                raise ProviderError("MAILBOX_UNAVAILABLE", retryable=True) from None
            except imaplib.IMAP4.error:
                raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED") from None
            yield client
        except (OSError, EOFError, imaplib.IMAP4.abort):
            raise ProviderError("MAILBOX_UNAVAILABLE", retryable=True) from None
        except (imaplib.IMAP4.error, ValueError, KeyError, TypeError, RecursionError):
            raise ProviderError("IMAP_RESPONSE_INVALID") from None
        finally:
            if client:
                with suppress(Exception):
                    client.logout()

    @staticmethod
    def _select(client: imaplib.IMAP4_SSL, folder: str) -> str:
        if folder.upper() != "INBOX":
            raise ProviderError("INVALID_FOLDER")
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise ProviderError("MAILBOX_FOLDER_UNAVAILABLE")
        _, values = client.response("UIDVALIDITY")
        value = values[0] if values else None
        if not isinstance(value, bytes) or not value.isdigit():
            raise ProviderError("IMAP_RESPONSE_INVALID")
        return value.decode("ascii")

    def login(self, email: str, password: str) -> MailboxGrant:
        value = "".join(password.split())
        if len(value) != 16 or not value.isascii():
            raise ProviderError("INVALID_APP_PASSWORD")
        credentials = {"email": email.strip().casefold(), "app_password": value}
        with self._session(credentials) as client:
            self._select(client, "INBOX")
        return MailboxGrant(credentials["email"], credentials["email"], credentials)

    def folders(self, credentials: dict[str, Any]) -> list[dict[str, str]]:
        with self._session(credentials) as client:
            self._select(client, "INBOX")
        return [{"id": "INBOX", "name": "Inbox"}]

    def baseline(self, credentials: dict[str, Any], folder: str) -> dict[str, Any]:
        with self._session(credentials) as client:
            validity = self._select(client, folder)
        # First poll must still find mail received between activation and this request.
        return {"uidvalidity": validity, "last_uid": 0}

    @staticmethod
    def _metadata(client: imaplib.IMAP4_SSL, uid: int) -> tuple[datetime, int] | None:
        status, rows = client.uid("fetch", str(uid), "(UID INTERNALDATE RFC822.SIZE)")
        if status != "OK":
            raise ProviderError("IMAP_RESPONSE_INVALID")
        for row in rows:
            if not isinstance(row, bytes):
                continue
            identity = re.search(rb"\bUID (\d+)", row)
            date = re.search(rb'INTERNALDATE "([^"]+)"', row)
            size = re.search(rb"RFC822.SIZE (\d+)", row)
            if identity and int(identity[1]) == uid and date and size:
                received = datetime.strptime(
                    date[1].decode("ascii").strip(), "%d-%b-%Y %H:%M:%S %z"
                )
                return received.astimezone(UTC), int(size[1])
        # Expunged during polling: no sequence-number substitution.
        return None

    @staticmethod
    def _raw(client: imaplib.IMAP4_SSL, uid: int, size: int) -> EmailMessage:
        if size > MAX_MESSAGE_BYTES:
            raise ProviderError("MAIL_MESSAGE_TOO_LARGE")
        status, rows = client.uid(
            "fetch", str(uid), f"(UID BODY.PEEK[]<0.{MAX_MESSAGE_BYTES + 1}>)"
        )
        if status != "OK":
            raise ProviderError("MAIL_MESSAGE_UNAVAILABLE")
        # IMAP servers may place UID after the body literal, regardless of request order.
        metadata = b" ".join(
            row[0] if isinstance(row, tuple) else row
            for row in rows
            if isinstance(row, bytes) or (isinstance(row, tuple) and isinstance(row[0], bytes))
        )
        identities = {int(value) for value in re.findall(rb"\bUID (\d+)", metadata)}
        for row in rows:
            if not isinstance(row, tuple) or len(row) != 2 or not isinstance(row[1], bytes):
                continue
            identity = re.search(rb"\bUID (\d+)", row[0])
            if (identity and int(identity[1]) != uid) or (not identity and identities != {uid}):
                continue
            if len(row[1]) > MAX_MESSAGE_BYTES:
                raise ProviderError("MAIL_MESSAGE_TOO_LARGE")
            if len(row[1]) != size:
                raise ProviderError("IMAP_RESPONSE_INVALID")
            return BytesParser(policy=policy.default).parsebytes(row[1])
        raise ProviderError("MAIL_MESSAGE_UNAVAILABLE")

    @staticmethod
    def _message(message: EmailMessage, identity: str, received: datetime) -> RemoteMessage:
        attachments = []
        reply = ""
        for position, part in enumerate(message.walk()):
            filename = part.get_filename()
            if not part.is_multipart() and filename:
                content = part.get_payload(decode=True)
                disposition = part.get_content_disposition()
                attachments.append(
                    RemoteAttachment(
                        str(position),
                        filename,
                        part.get_content_type(),
                        len(content) if isinstance(content, bytes) else 0,
                        inline=disposition == "inline"
                        or (disposition != "attachment" and part.get("Content-ID") is not None),
                    )
                )
            elif not part.is_multipart() and part.get_content_type() == "text/plain" and not reply:
                content = part.get_payload(decode=True)
                if isinstance(content, bytes):
                    # No HTML rendering or remote-resource access.
                    reply = content[:8192].decode("utf-8", errors="replace")
        return RemoteMessage(
            id=identity,
            received_at=received,
            internet_message_id=str(message["Message-ID"]) if message["Message-ID"] else None,
            attachments=attachments,
            in_reply_to=str(message["In-Reply-To"]) if message["In-Reply-To"] else None,
            reply_text=reply,
            sender=parseaddr(str(message.get("From", "")))[1].casefold(),
            automated=str(message.get("Auto-Submitted", "no")).casefold() != "no",
            bounce=message.get_content_type() == "multipart/report",
        )

    def page(
        self, credentials: dict[str, Any], folder: str, cursor: dict[str, Any], after: datetime
    ) -> MessagePage:
        with self._session(credentials) as client:
            validity = self._select(client, folder)
            if validity != cursor["uidvalidity"]:
                raise CursorExpired
            last = int(cursor["last_uid"])
            # IMAP day-only search ignores time zones. Fetch exact INTERNALDATE before bodies.
            day = (after.astimezone(UTC) - timedelta(days=1)).strftime("%d-%b-%Y")
            status, rows = client.uid("search", "UID", f"{last + 1}:*", "SINCE", day)
            if status != "OK" or not rows or not isinstance(rows[0], bytes):
                raise ProviderError("IMAP_RESPONSE_INVALID")
            uids = sorted({int(value) for value in rows[0].split() if int(value) > last})
            messages = []
            for uid in uids[:PAGE_SIZE]:
                metadata = self._metadata(client, uid)
                if metadata and metadata[0] >= after:
                    raw = self._raw(client, uid, metadata[1])
                    messages.append(self._message(raw, f"{validity}:{uid}", metadata[0]))
                last = uid
        return MessagePage(
            messages, {"uidvalidity": validity, "last_uid": last}, len(uids) <= PAGE_SIZE
        )

    def attachment(self, credentials: dict[str, Any], message: str, attachment: str) -> bytes:
        match = re.fullmatch(r"(\d+):(\d+)", message)
        if not match or not attachment.isdigit():
            raise ProviderError("MAIL_MESSAGE_UNAVAILABLE")
        with self._session(credentials) as client:
            if self._select(client, "INBOX") != match[1]:
                raise ProviderError("MAIL_MESSAGE_UNAVAILABLE")
            uid = int(match[2])
            metadata = self._metadata(client, uid)
            if not metadata:
                raise ProviderError("MAIL_MESSAGE_UNAVAILABLE")
            raw = self._raw(client, uid, metadata[1])
            for position, part in enumerate(raw.walk()):
                if position == int(attachment) and not part.is_multipart() and part.get_filename():
                    content = part.get_payload(decode=True)
                    if isinstance(content, bytes):
                        return content
        raise ProviderError("MAIL_ATTACHMENT_UNAVAILABLE")

    def disconnect(self, credentials: dict[str, Any], subscription: str | None) -> None:
        # No subscription to cancel. The service deletes the encrypted credential.
        pass
