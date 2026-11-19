from typing import Any

from recruitment.adapters.gmail import GmailMailboxProvider
from recruitment.adapters.gmail_imap import GmailIMAPProvider
from recruitment.adapters.mail_fake import FakeIMAPProvider, FakeMailboxProvider
from recruitment.adapters.microsoft import MicrosoftMailboxProvider
from recruitment.adapters.screening_email import FakeScreeningEmail, GmailImapScreeningEmail
from recruitment.adapters.vault import credential_vault
from recruitment.core.config import Settings
from recruitment.core.errors import AppError, ProviderError
from recruitment.ports.mailbox import (
    CredentialVault,
    MailboxProvider,
    MailboxReader,
    PasswordMailboxProvider,
)
from recruitment.ports.screening import EmailProvider

SCREENING_MAILBOX_METHODS = frozenset({("gmail", "imap")})


class MailboxRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.imap: PasswordMailboxProvider = (
            FakeIMAPProvider()
            if settings.mailbox_provider_mode == "fake"
            else GmailIMAPProvider(settings)
        )
        self.providers: dict[str, MailboxProvider] = (
            {name: FakeMailboxProvider(name) for name in ("gmail", "microsoft")}
            if settings.mailbox_provider_mode == "fake"
            else {
                "gmail": GmailMailboxProvider(settings),
                "microsoft": MicrosoftMailboxProvider(settings),
            }
        )
        self.fake_screening_emails: dict[str, FakeScreeningEmail] = {}

    @property
    def vault(self) -> CredentialVault:
        return credential_vault(self.settings)

    def configured(self, name: str) -> bool:
        return self.method_configured(f"{name}_oauth")

    def ensure_enabled(self, method: str) -> None:
        if not self.settings.mailbox_method_enabled(method):
            raise AppError(
                "MAILBOX_METHOD_DISABLED",
                "This connection method is disabled. Connect through an available OAuth provider.",
                403,
            )

    def reader(self, provider: str, method: str) -> MailboxReader:
        return self.imap if method == "imap" else self.providers[provider]

    def screening_email(
        self, provider: str, method: str, address: str, credentials: dict[str, Any]
    ) -> EmailProvider:
        if (provider, method) not in SCREENING_MAILBOX_METHODS:
            raise ProviderError("SCREENING_MAILBOX_READ_ONLY")
        if self.settings.mailbox_provider_mode == "fake":
            if address not in self.fake_screening_emails:
                self.fake_screening_emails[address] = FakeScreeningEmail(address)
            return self.fake_screening_emails[address]
        if (
            credentials.get("email") != address
            or not credentials.get("app_password")
            or not isinstance(self.imap, GmailIMAPProvider)
        ):
            raise ProviderError("SCREENING_MAILBOX_RECONNECT")
        return GmailImapScreeningEmail(self.imap, address, credentials)

    def method_configured(self, method: str) -> bool:
        if not self.settings.mailbox_method_enabled(method):
            return False
        if self.settings.mailbox_provider_mode == "fake":
            return True
        if not self.settings.credential_keys:
            return False
        if method == "imap":
            return True
        if not self.settings.public_webhook_url:
            return False
        if method == "gmail_oauth":
            return bool(
                self.settings.gmail_client_id
                and self.settings.gmail_client_secret
                and self.settings.gmail_pubsub_topic
                and self.settings.gmail_push_audience
                and self.settings.gmail_push_service_account
            )
        return bool(self.settings.microsoft_client_id and self.settings.microsoft_client_secret)
