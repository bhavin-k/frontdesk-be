import re
import smtplib
import ssl
from contextlib import suppress
from datetime import datetime
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import formatdate, parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from recruitment.adapters.gmail_imap import GmailIMAPProvider
from recruitment.core.errors import ProviderError
from recruitment.ports.screening import InboxCursor, IncomingEmail, OutgoingEmail

MESSAGE_IDS = re.compile(r"<[^<>\s]{1,510}>")
INTERNAL_DATE = re.compile(rb'INTERNALDATE "([^"]+)"')


def message_ids(value: object) -> tuple[str, ...]:
    return tuple(MESSAGE_IDS.findall(str(value or ""))[:100])


class TextContent(HTMLParser):
    hidden_tags = {"blockquote", "script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden_tag: str | None = None
        self.hidden_depth = 0

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.hidden_tag:
            if tag == self.hidden_tag:
                self.hidden_depth += 1
            return
        classes = (dict(attrs).get("class") or "").split()
        if tag in self.hidden_tags or {"gmail_quote", "gmail_attr"}.intersection(classes):
            self.hidden_tag = tag
            self.hidden_depth += 1
        elif tag in {"br", "p", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == self.hidden_tag:
            self.hidden_depth -= 1
            if not self.hidden_depth:
                self.hidden_tag = None


def parse_inbox_message(
    raw: bytes,
    received_at: datetime | None = None,
) -> IncomingEmail | None:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    identifiers = message_ids(message.get("Message-ID"))
    if not identifiers:
        return None
    parent = message_ids(message.get("In-Reply-To"))
    body = message.get_body(preferencelist=("plain", "html"))
    try:
        content = str(body.get_content(errors="replace")) if body else ""
    except (LookupError, TypeError, ValueError):
        content = ""
    if body and body.get_content_type() == "text/html":
        parser = TextContent()
        parser.feed(content)
        content = "".join(parser.parts)
    bounced: list[str] = []
    failed_recipient = None
    if message.get_content_type() == "multipart/report":
        for part in message.walk():
            if part.get_content_type() == "message/delivery-status":
                payload = part.get_payload()
                if isinstance(payload, list):
                    for report in payload:
                        if not isinstance(report, Message):
                            continue
                        if str(report.get("Action", "")).strip().casefold() == "failed":
                            recipient = str(report.get("Final-Recipient", ""))
                            failed_recipient = recipient.partition(";")[2].strip()
            elif part.get_content_type() in {"message/rfc822", "text/rfc822-headers"}:
                embedded = part.get_payload()
                if isinstance(embedded, list):
                    for child in embedded:
                        if isinstance(child, Message):
                            bounced.extend(message_ids(child.get("Message-ID")))
                else:
                    payload = part.get_payload(decode=True)
                    if isinstance(payload, bytes):
                        headers = BytesParser(policy=policy.default).parsebytes(
                            payload, headersonly=True
                        )
                        bounced.extend(message_ids(headers.get("Message-ID")))
    return IncomingEmail(
        message_id=identifiers[0],
        sender=parseaddr(str(message.get("From", "")))[1].casefold(),
        body=content[:24_000].strip(),
        in_reply_to=parent[0] if parent else None,
        references=message_ids(message.get("References")),
        bounced_message_ids=tuple(bounced) if failed_recipient else (),
        bounced_recipient=failed_recipient,
        automated=(
            message.get_content_type() == "multipart/report"
            or str(message.get("Auto-Submitted", "no")).strip().casefold() != "no"
            or str(message.get("Precedence", "")).casefold() in {"bulk", "list", "junk"}
        ),
        received_at=received_at,
    )


def smtp_failure(code: int) -> ProviderError:
    if 400 <= code < 500:
        return ProviderError("SCREENING_SMTP_TEMPORARY", retryable=True)
    return ProviderError("SCREENING_SMTP_REJECTED")


class GmailImapScreeningEmail:
    def __init__(self, imap: GmailIMAPProvider, address: str, credentials: dict[str, Any]) -> None:
        self.imap = imap
        self.address = address
        self.credentials = credentials

    def send(self, message: OutgoingEmail) -> None:
        outgoing = EmailMessage()
        outgoing["From"] = self.address
        outgoing["To"] = message.recipient
        outgoing["Subject"] = message.subject
        outgoing["Date"] = formatdate(usegmt=True)
        outgoing["Message-ID"] = message.message_id
        outgoing["Auto-Submitted"] = "auto-generated"
        if message.in_reply_to:
            outgoing["In-Reply-To"] = message.in_reply_to
        if message.references:
            outgoing["References"] = " ".join(message.references)
        outgoing.set_content(message.body)
        client: smtplib.SMTP | None = None
        try:
            try:
                client = smtplib.SMTP_SSL(
                    "smtp.gmail.com", 465, timeout=15, context=ssl.create_default_context()
                )
                client.login(self.address, self.credentials["app_password"])
            except smtplib.SMTPResponseException as exc:
                if isinstance(exc, smtplib.SMTPAuthenticationError):
                    raise ProviderError("SCREENING_SMTP_AUTH") from exc
                raise smtp_failure(exc.smtp_code) from exc
            except smtplib.SMTPNotSupportedError as exc:
                raise ProviderError("SCREENING_SMTP_TLS_REQUIRED") from exc
            except OSError as exc:
                raise ProviderError("SCREENING_SMTP_CONNECT", retryable=True) from exc
            try:
                client.send_message(outgoing, from_addr=self.address, to_addrs=[message.recipient])
            except smtplib.SMTPRecipientsRefused as exc:
                code, _ = next(iter(exc.recipients.values()))
                raise smtp_failure(code) from exc
            except smtplib.SMTPResponseException as exc:
                raise smtp_failure(exc.smtp_code) from exc
            except smtplib.SMTPNotSupportedError as exc:
                raise ProviderError("SCREENING_SMTP_ADDRESS_UNSUPPORTED") from exc
            except OSError as exc:
                # DATA could have reached the server before the response was lost.
                raise ProviderError("SCREENING_SEND_UNCERTAIN") from exc
        finally:
            if client is not None:
                with suppress(OSError):
                    client.close()

    def reply_in_thread(self, message: OutgoingEmail) -> None:
        self.send(message)

    def fetch_new(
        self, cursor: InboxCursor, after: datetime
    ) -> tuple[list[IncomingEmail], InboxCursor]:
        with self.imap.inbox(self.credentials) as (client, validity):
            uid_validity = int(validity)
            last = int(cursor.get("uid", 0)) if cursor.get("validity") == uid_validity else 0
            messages = []
            for uid in self.imap.uids_since(client, last, after)[:100]:
                status, items = client.uid(
                    "fetch", str(uid), "(INTERNALDATE BODY.PEEK[]<0.262144>)"
                )
                if status != "OK":
                    raise ProviderError("SCREENING_IMAP_FETCH", retryable=True)
                for item in items:
                    if isinstance(item, tuple):
                        received = None
                        if match := INTERNAL_DATE.search(item[0]):
                            with suppress(ValueError):
                                received = parsedate_to_datetime(match[1].decode("ascii"))
                        if received and received.tzinfo and received < after:
                            continue
                        if message := parse_inbox_message(item[1], received):
                            messages.append(message)
                last = uid
            return messages, {"validity": uid_validity, "uid": last}


class FakeScreeningEmail:
    def __init__(self, address: str) -> None:
        self.address = address
        self.sent: list[OutgoingEmail] = []
        self.inbox: list[IncomingEmail] = []

    def send(self, message: OutgoingEmail) -> None:
        self.sent.append(message)

    def reply_in_thread(self, message: OutgoingEmail) -> None:
        self.send(message)

    def fetch_new(
        self, cursor: InboxCursor, after: datetime
    ) -> tuple[list[IncomingEmail], InboxCursor]:
        return self.inbox[int(cursor.get("uid", 0)) :], {"validity": 1, "uid": len(self.inbox)}
