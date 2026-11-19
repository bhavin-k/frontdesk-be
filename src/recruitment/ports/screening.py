from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from recruitment.domain.screening import ReplyDraft, ReplyIntent, Slot


@dataclass(frozen=True)
class OutgoingEmail:
    recipient: str
    subject: str
    body: str
    message_id: str
    in_reply_to: str | None
    references: tuple[str, ...]


@dataclass(frozen=True)
class IncomingEmail:
    message_id: str
    sender: str
    body: str
    in_reply_to: str | None = None
    references: tuple[str, ...] = ()
    bounced_message_ids: tuple[str, ...] = ()
    bounced_recipient: str | None = None
    automated: bool = False
    received_at: datetime | None = None


type InboxCursor = dict[str, str | int]


class EmailProvider(Protocol):
    @property
    def address(self) -> str: ...

    def send(self, message: OutgoingEmail) -> None: ...
    def reply_in_thread(self, message: OutgoingEmail) -> None: ...
    def fetch_new(
        self, cursor: InboxCursor, after: datetime
    ) -> tuple[list[IncomingEmail], InboxCursor]: ...


class AvailabilityProvider(Protocol):
    def timezone(self, tenant_timezone: str) -> str: ...
    def slots(self, earliest: datetime, deadline: datetime, tenant_timezone: str) -> list[Slot]: ...
    def validate(self, start: datetime, tenant_timezone: str) -> Slot | None: ...


class ReplyClassifier(Protocol):
    def interpret(self, thread: str) -> ReplyIntent: ...


class ReplyDrafter(Protocol):
    def draft(self, context: str) -> ReplyDraft: ...
