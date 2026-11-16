"""Exercise real IMAP parsing and commands through an in-memory protocol fake."""

import base64
import imaplib
import json
import secrets
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr, ValidationError

from recruitment.adapters import gmail_imap
from recruitment.adapters.gmail_imap import MAX_MESSAGE_BYTES, GmailIMAPProvider
from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError
from recruitment.ports.mailbox import CursorExpired


class Inbox:
    def __init__(self):
        self.validity = b"77"
        self.calls = []
        self.rows = {}
        self.logged_out = False
        self.debug = 0

    def login(self, email, password):
        return "OK", []

    def select(self, folder, readonly):
        assert folder == "INBOX" and readonly
        return "OK", [b"2"]

    def response(self, name):
        assert name == "UIDVALIDITY"
        return name, [self.validity]

    def logout(self):
        self.logged_out = True

    def uid(self, command, *args):
        self.calls.append((command, args))
        if command == "search":
            # Include the lower-bound UID: IMAP's '*' range can return this again.
            return "OK", [b"1 2"]
        uid, query = int(args[0]), args[1]
        received, raw = self.rows[uid]
        stamp = received.strftime("%d-%b-%Y %H:%M:%S %z")
        prefix = f'1 (UID {uid} INTERNALDATE "{stamp}" RFC822.SIZE {len(raw)})'.encode()
        if query == "(UID INTERNALDATE RFC822.SIZE)":
            return "OK", [prefix]
        assert "BODY.PEEK[]" in query
        return "OK", [(f"1 (UID {uid} BODY[] {{0}}".encode(), raw), b")"]


def email_bytes():
    value = EmailMessage()
    value["From"] = "Candidate <candidate@example.org>"
    value["Message-ID"] = "<candidate-cv@example.org>"
    value["Subject"] = "CV"
    value.set_content("Please find my CV attached.")
    value.add_attachment(
        b"%PDF-fictional-CV", maintype="application", subtype="pdf", filename="cv.pdf"
    )
    return value.as_bytes()


def setup_provider(settings, monkeypatch):
    inbox = Inbox()
    factory = MagicMock(return_value=inbox)
    monkeypatch.setattr(gmail_imap.imaplib, "IMAP4_SSL", factory)
    provider = GmailIMAPProvider(settings)
    credentials = {"email": "owner@gmail.com", "app_password": "test" * 4}
    return provider, inbox, factory, credentials


def test_imap_tls_readonly_exact_cutoff_and_uid_resume(settings, monkeypatch):
    provider, inbox, factory, credentials = setup_provider(settings, monkeypatch)
    cutoff = datetime(2026, 11, 4, 8, 0, tzinfo=UTC)
    inbox.rows = {
        1: (cutoff - timedelta(seconds=1), email_bytes()),
        2: (cutoff + timedelta(seconds=1), email_bytes()),
    }
    cursor = provider.baseline(credentials, "INBOX")
    page = provider.page(credentials, "INBOX", cursor, cutoff)
    assert [row.id for row in page.messages] == ["77:2"]
    message = page.messages[0]
    assert message.sender == "candidate@example.org"
    assert message.attachments[0].filename == "cv.pdf"
    assert not any(
        command == "fetch" and args[0] == "1" and "BODY.PEEK" in args[1]
        for command, args in inbox.calls
    )
    assert page.complete and page.cursor == {"uidvalidity": "77", "last_uid": 2}
    assert not provider.page(credentials, "INBOX", page.cursor, cutoff).messages
    assert (
        provider.attachment(credentials, message.id, message.attachments[0].id)
        == b"%PDF-fictional-CV"
    )
    assert factory.call_args.args == ("imap.gmail.com", 993)
    assert factory.call_args.kwargs["ssl_context"].check_hostname
    assert factory.call_args.kwargs["timeout"] == 10
    assert inbox.logged_out


def test_imap_uidvalidity_fences_attachment_and_triggers_cursor_recovery(settings, monkeypatch):
    provider, inbox, _, credentials = setup_provider(settings, monkeypatch)
    inbox.validity = b"88"
    with pytest.raises(CursorExpired):
        provider.page(credentials, "INBOX", {"uidvalidity": "77", "last_uid": 0}, datetime.now(UTC))
    with pytest.raises(ProviderError, match="MAIL_MESSAGE_UNAVAILABLE"):
        provider.attachment(credentials, "77:1", "2")
    assert not inbox.calls


@pytest.mark.parametrize(
    "error,retry,code",
    [
        (imaplib.IMAP4.error("sensitive login error"), False, "MAILBOX_REAUTHORIZATION_REQUIRED"),
        (TimeoutError("sensitive timeout"), True, "MAILBOX_UNAVAILABLE"),
    ],
)
def test_imap_failures_are_sanitized(settings, monkeypatch, error, retry, code):
    provider, inbox, _, _ = setup_provider(settings, monkeypatch)
    inbox.login = MagicMock(side_effect=error)
    with pytest.raises(ProviderError) as failure:
        provider.login("owner@gmail.com", "test" * 4)
    assert failure.value.code == code and failure.value.retryable == retry
    assert "sensitive" not in str(failure.value)
    assert inbox.logged_out


def test_imap_rejects_oversized_and_incomplete_messages(settings, monkeypatch):
    provider, inbox, _, credentials = setup_provider(settings, monkeypatch)
    with pytest.raises(ProviderError, match="MAIL_MESSAGE_TOO_LARGE"):
        provider._raw(inbox, 2, MAX_MESSAGE_BYTES + 1)
    inbox.rows = {2: (datetime.now(UTC), email_bytes())}
    with pytest.raises(ProviderError, match="IMAP_RESPONSE_INVALID"):
        provider._raw(inbox, 2, 1)
    with pytest.raises(ProviderError, match="INVALID_APP_PASSWORD"):
        provider.login("owner@gmail.com", "short")
    with pytest.raises(ProviderError, match="INVALID_FOLDER"):
        provider.baseline(credentials, "other-folder")


def test_imap_accepts_uid_after_body_literal_without_using_message_sequence(settings, monkeypatch):
    provider, inbox, _, _ = setup_provider(settings, monkeypatch)
    raw = email_bytes()
    inbox.uid = lambda *args: ("OK", [(b"99 (BODY[] {0}", raw), b" UID 123)"])
    assert provider._raw(inbox, 123, len(raw))["Message-ID"] == "<candidate-cv@example.org>"
    with pytest.raises(ProviderError, match="MAIL_MESSAGE_UNAVAILABLE"):
        provider._raw(inbox, 99, len(raw))


def test_imap_pages_without_dropping_messages_during_initial_sync(settings, monkeypatch):
    provider, inbox, _, credentials = setup_provider(settings, monkeypatch)
    cutoff = datetime.now(UTC) - timedelta(minutes=1)
    inbox.rows = {number: (cutoff + timedelta(seconds=1), email_bytes()) for number in range(1, 28)}
    original = inbox.uid
    inbox.uid = lambda command, *args: (
        ("OK", [b" ".join(str(x).encode() for x in range(1, 28))])
        if command == "search"
        else original(command, *args)
    )
    first = provider.page(credentials, "INBOX", {"uidvalidity": "77", "last_uid": 0}, cutoff)
    assert not first.complete and len(first.messages) == 25
    second = provider.page(credentials, "INBOX", first.cursor, cutoff)
    assert second.complete and [x.id for x in second.messages] == ["77:26", "77:27"]


@pytest.mark.parametrize("methods", ["smtp", "imap,typo", "imap,imap"])
def test_mailbox_methods_reject_invalid_settings(settings, methods):
    with pytest.raises(ValidationError):
        Settings(**{**settings.model_dump(), "enabled_mailbox_methods": methods})


def test_imap_is_opt_in_and_forbidden_in_production(settings):
    assert not settings.mailbox_method_enabled("imap")
    with pytest.raises(ValidationError, match="development and testing only"):
        Settings(
            **{
                **settings.model_dump(),
                "environment": "production",
                "provider_mode": "real",
                "mailbox_provider_mode": "real",
                "enabled_mailbox_methods": "imap",
            }
        )
    disabled = Settings(**{**settings.model_dump(), "enabled_mailbox_methods": ""})
    assert not any(
        disabled.mailbox_method_enabled(x) for x in ("imap", "gmail_oauth", "microsoft_oauth")
    )


def test_real_imap_needs_no_oauth_registration_or_public_webhook(settings):
    settings.mailbox_provider_mode = "real"
    settings.enabled_mailbox_methods = "imap,gmail_oauth,microsoft_oauth"
    runtime = MailboxRuntime(settings)
    assert not runtime.method_configured("imap")
    settings.credential_keys = SecretStr(
        json.dumps({"1": base64.b64encode(secrets.token_bytes(32)).decode()})
    )
    assert runtime.method_configured("imap")
    assert not runtime.configured("gmail") and not runtime.configured("microsoft")


@pytest.mark.parametrize("blank", ["", "   "])
def test_dotenv_can_leave_the_unused_imap_webhook_empty(settings, tmp_path, blank):
    path = tmp_path / ".env"
    path.write_text(f"RECRUITMENT_PUBLIC_WEBHOOK_URL={blank}\n", encoding="utf-8")
    values = settings.model_dump(exclude={"public_webhook_url"})
    values["environment"] = "development"
    configured = Settings(_env_file=path, **values)
    assert configured.public_webhook_url is None


@pytest.mark.parametrize("url", ["http://public.example.org", "file:///tmp/webhook"])
def test_nonempty_webhook_urls_still_require_https(settings, url):
    with pytest.raises(ValidationError, match="Public URLs require HTTPS"):
        Settings(**{**settings.model_dump(), "public_webhook_url": url})
