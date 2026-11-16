"""Deterministic mailbox contract fake. Never connects to an external inbox."""

from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from recruitment.core.time import utcnow
from recruitment.ports.mailbox import (
    AuthorizationStart,
    MailboxGrant,
    MessagePage,
    RemoteMessage,
    Subscription,
)


class FakeMailboxProvider:
    def __init__(self, name: str) -> None:
        self.name = name
        self.messages: list[RemoteMessage] = []
        self.files: dict[tuple[str, str], bytes] = {}
        self.downloads: list[tuple[str, str]] = []
        self.owner: str | None = None

    def authorize(self, state: str, redirect_uri: str) -> AuthorizationStart:
        return AuthorizationStart(
            f"{redirect_uri}?{urlencode({'state': state, 'code': 'local-fake'})}", {"state": state}
        )

    def exchange(self, flow: dict[str, Any], code: str, redirect_uri: str) -> MailboxGrant:
        from recruitment.core.errors import ProviderError

        if code != "local-fake":
            raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED")
        owner = (
            self.owner
            or f"recruiter-{flow.get('_fake_recruiter_id', 'local')}@{self.name}.example.invalid"
        )
        return MailboxGrant(owner, owner, {"mode": "local-fake"})

    def folders(self, credentials: dict[str, Any]) -> list[dict[str, str]]:
        return [
            {"id": "INBOX" if self.name == "gmail" else "inbox", "name": "Inbox"},
            {"id": "cv-folder", "name": "CVs"},
        ]

    def baseline(self, credentials: dict[str, Any], folder: str) -> dict[str, Any]:
        return {"offset": 0}

    def page(
        self, credentials: dict[str, Any], folder: str, cursor: dict[str, Any], after: datetime
    ) -> MessagePage:
        start = int(cursor.get("offset", 0))
        return MessagePage(
            [item for item in self.messages[start:] if item.received_at >= after],
            {"offset": len(self.messages)},
            True,
        )

    def attachment(self, credentials: dict[str, Any], message: str, attachment: str) -> bytes:
        self.downloads.append((message, attachment))
        return self.files[(message, attachment)]

    def subscribe(
        self,
        credentials: dict[str, Any],
        folder: str,
        callback: str,
        secret: str,
        previous: str | None,
    ) -> Subscription:
        return Subscription(previous or "local-fake-subscription", utcnow() + timedelta(days=3))

    def disconnect(self, credentials: dict[str, Any], subscription: str | None) -> None:
        pass


class FakeIMAPProvider(FakeMailboxProvider):
    def __init__(self) -> None:
        super().__init__("gmail")

    def login(self, email: str, password: str) -> MailboxGrant:
        # Do not retain even fictional submitted passwords in fake storage.
        return MailboxGrant(email, email, {"mode": "local-fake"})

    def folders(self, credentials: dict[str, Any]) -> list[dict[str, str]]:
        return [{"id": "INBOX", "name": "Inbox"}]
