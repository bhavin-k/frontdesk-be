from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Sealed:
    key_id: str
    nonce: bytes
    ciphertext: bytes


class CredentialVault(Protocol):
    def seal(self, value: dict[str, Any], context: str) -> Sealed: ...
    def open(self, value: Sealed, context: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AuthorizationStart:
    url: str
    flow: dict[str, Any]


@dataclass(frozen=True)
class MailboxGrant:
    owner_id: str
    email: str
    credentials: dict[str, Any]


@dataclass(frozen=True)
class RemoteAttachment:
    id: str
    filename: str
    mime_type: str
    size: int


@dataclass(frozen=True)
class RemoteMessage:
    id: str
    received_at: datetime
    internet_message_id: str | None = None
    attachments: list[RemoteAttachment] = field(default_factory=list)
    in_reply_to: str | None = None
    reply_text: str = ""
    bounce: bool = False
    sender: str = ""
    automated: bool = False
    thread_id: str | None = None


@dataclass(frozen=True)
class MessagePage:
    messages: list[RemoteMessage]
    cursor: dict[str, Any]
    complete: bool


@dataclass(frozen=True)
class Subscription:
    id: str
    expires_at: datetime


class CursorExpired(Exception):
    pass


class MailboxReader(Protocol):
    name: str

    def folders(self, credentials: dict[str, Any]) -> list[dict[str, str]]: ...
    def baseline(self, credentials: dict[str, Any], folder: str) -> dict[str, Any]: ...
    def page(
        self, credentials: dict[str, Any], folder: str, cursor: dict[str, Any], after: datetime
    ) -> MessagePage: ...
    def attachment(self, credentials: dict[str, Any], message: str, attachment: str) -> bytes: ...
    def disconnect(self, credentials: dict[str, Any], subscription: str | None) -> None: ...


class PasswordMailboxProvider(MailboxReader, Protocol):
    def login(self, email: str, password: str) -> MailboxGrant: ...


class MailboxProvider(MailboxReader, Protocol):
    def authorize(self, state: str, redirect_uri: str) -> AuthorizationStart: ...
    def exchange(self, flow: dict[str, Any], code: str, redirect_uri: str) -> MailboxGrant: ...
    def subscribe(
        self,
        credentials: dict[str, Any],
        folder: str,
        callback: str,
        secret: str,
        previous: str | None,
    ) -> Subscription: ...
