import base64
import secrets
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from recruitment.adapters.mail_http import provider_request
from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.ports.mailbox import (
    AuthorizationStart,
    CursorExpired,
    MailboxGrant,
    MessagePage,
    RemoteAttachment,
    RemoteMessage,
    Subscription,
)

ORIGIN = "https://gmail.googleapis.com/gmail/v1"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class GmailMailboxProvider:
    name = "gmail"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _flow(self, redirect_uri: str, verifier: str, state: str) -> Any:
        if not self.settings.gmail_client_id or not self.settings.gmail_client_secret:
            raise ProviderError("GMAIL_NOT_CONFIGURED")
        return Flow.from_client_config(
            {
                "web": {
                    "client_id": self.settings.gmail_client_id,
                    "client_secret": self.settings.gmail_client_secret.get_secret_value(),
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=SCOPES,
            redirect_uri=redirect_uri,
            code_verifier=verifier,
            state=state,
        )

    def authorize(self, state: str, redirect_uri: str) -> AuthorizationStart:
        verifier = secrets.token_urlsafe(64)
        flow = self._flow(redirect_uri, verifier, state)
        url, _ = flow.authorization_url(access_type="offline", prompt="consent")
        return AuthorizationStart(str(url), {"verifier": verifier, "state": state})

    def exchange(self, flow: dict[str, Any], code: str, redirect_uri: str) -> MailboxGrant:
        try:
            oauth = self._flow(redirect_uri, flow["verifier"], flow["state"])
            oauth.fetch_token(code=code, timeout=30)
            credentials: dict[str, Any] = {
                "token": oauth.credentials.token,
                "refresh_token": oauth.credentials.refresh_token,
                "expiry": oauth.credentials.expiry.replace(tzinfo=UTC).isoformat()
                if oauth.credentials.expiry
                else None,
            }
            _, profile = self._request(credentials, "GET", "users/me/profile")
            email = str(profile["emailAddress"]).strip().casefold()
            return MailboxGrant(email, email, credentials)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED") from exc

    def _token(self, credentials: dict[str, Any]) -> str:
        expiry = (
            datetime.fromisoformat(credentials["expiry"]) if credentials.get("expiry") else utcnow()
        )
        if expiry <= utcnow() + timedelta(minutes=1):
            try:
                value = Credentials(  # type: ignore[no-untyped-call]
                    token=credentials.get("token"),
                    refresh_token=credentials.get("refresh_token"),
                    token_uri="https://oauth2.googleapis.com/token",
                    client_id=self.settings.gmail_client_id,
                    client_secret=self.settings.gmail_client_secret.get_secret_value()
                    if self.settings.gmail_client_secret
                    else None,
                    scopes=SCOPES,
                )
                value.refresh(GoogleRequest())  # type: ignore[no-untyped-call]
                if not value.expiry or not value.token:
                    raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED")
                credentials.update(
                    token=value.token, expiry=value.expiry.replace(tzinfo=UTC).isoformat()
                )
                if value.refresh_token:
                    credentials["refresh_token"] = value.refresh_token
            except Exception as exc:
                raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED") from exc
        return str(credentials["token"])

    def _request(
        self, credentials: dict[str, Any], method: str, path: str, **kwargs: Any
    ) -> tuple[int, Any]:
        return provider_request(ORIGIN, method, path, self._token(credentials), **kwargs)

    def folders(self, credentials: dict[str, Any]) -> list[dict[str, str]]:
        _, result = self._request(credentials, "GET", "users/me/labels")
        return [
            {"id": str(row["id"]), "name": str(row["name"])}
            for row in result.get("labels", [])
            if row["id"] not in {"SENT", "DRAFT", "TRASH", "SPAM"}
        ]

    def baseline(self, credentials: dict[str, Any], folder: str) -> dict[str, Any]:
        _, profile = self._request(credentials, "GET", "users/me/profile")
        return {"history_id": str(profile["historyId"]), "bootstrap": True}

    def _message(
        self, credentials: dict[str, Any], identifier: str, after: datetime, folder: str
    ) -> RemoteMessage | None:
        escaped = quote(identifier, safe="")
        status, metadata = self._request(
            credentials, "GET", f"users/me/messages/{escaped}", params={"format": "metadata"}
        )
        if status == 404:
            return None
        received = datetime.fromtimestamp(int(metadata["internalDate"]) / 1000, UTC)
        if (
            received < after
            or folder not in metadata.get("labelIds", [])
            or "SENT" in metadata.get("labelIds", [])
        ):
            return None
        status, message = self._request(
            credentials, "GET", f"users/me/messages/{escaped}", params={"format": "full"}
        )
        if status == 404:
            return None
        headers = {
            str(h["name"]).casefold(): str(h["value"])
            for h in message.get("payload", {}).get("headers", [])
        }
        attachments: list[RemoteAttachment] = []
        plain: list[str] = []

        def visit(part: dict[str, Any], path: str) -> None:
            body = part.get("body", {})
            if part.get("filename"):
                attachment_id = body.get("attachmentId") or f"inline:{path}"
                headers = {h["name"].lower(): h["value"] for h in part.get("headers", [])}
                disposition = (
                    headers.get("content-disposition", "").split(";", 1)[0].strip().lower()
                )
                attachments.append(
                    RemoteAttachment(
                        str(attachment_id),
                        str(part["filename"]),
                        str(part.get("mimeType", "")),
                        int(body.get("size", 0)),
                        inline=disposition == "inline"
                        or (disposition != "attachment" and "content-id" in headers),
                    )
                )
            elif (
                part.get("mimeType") == "text/plain"
                and body.get("data")
                and len(str(body["data"])) < 100000
            ):
                plain.append(decode(str(body["data"])).decode("utf-8", errors="replace"))
            for i, child in enumerate(part.get("parts", [])):
                visit(child, f"{path}.{i}")

        visit(message.get("payload", {}), "0")
        if len(attachments) > 100:
            raise ProviderError("MESSAGE_ATTACHMENT_LIMIT")
        return RemoteMessage(
            identifier,
            received,
            headers.get("message-id"),
            attachments,
            headers.get("in-reply-to"),
            "\n".join(plain)[:65536],
            "delivery-status" in headers.get("content-type", "").casefold(),
            sender=parseaddr(headers.get("from", ""))[1].casefold(),
            automated=headers.get("auto-submitted", "no").casefold() != "no"
            or headers.get("precedence", "").casefold() in {"bulk", "list", "junk"},
            thread_id=str(message.get("threadId", "")) or None,
        )

    def page(
        self, credentials: dict[str, Any], folder: str, cursor: dict[str, Any], after: datetime
    ) -> MessagePage:
        params: dict[str, Any] = {"maxResults": 100}
        if cursor.get("page"):
            params["pageToken"] = cursor["page"]
        ids: list[str] = []
        if cursor.get("bootstrap"):
            params.update(labelIds=folder, q=f"after:{int(after.timestamp()) - 1}")
            _, result = self._request(credentials, "GET", "users/me/messages", params=params)
            ids = [str(row["id"]) for row in result.get("messages", [])]
        else:
            params.update(startHistoryId=cursor["history_id"], labelId=folder)
            status, result = self._request(credentials, "GET", "users/me/history", params=params)
            if status == 404:
                raise CursorExpired
            for change in result.get("history", []):
                for kind in ("messagesAdded", "labelsAdded"):
                    ids.extend(str(item["message"]["id"]) for item in change.get(kind, []))
        messages = [
            message
            for identifier in dict.fromkeys(ids)
            if (message := self._message(credentials, identifier, after, folder)) is not None
        ]
        next_cursor = dict(cursor)
        if result.get("nextPageToken"):
            next_cursor["page"] = result["nextPageToken"]
            return MessagePage(messages, next_cursor, False)
        next_cursor.pop("page", None)
        next_cursor.pop("bootstrap", None)
        # A watch's history ID must never overwrite the import cursor.
        if not cursor.get("bootstrap") and result.get("historyId"):
            next_cursor["history_id"] = str(result["historyId"])
        return MessagePage(messages, next_cursor, True)

    def attachment(self, credentials: dict[str, Any], message: str, attachment: str) -> bytes:
        path = f"users/me/messages/{quote(message, safe='')}"
        if attachment.startswith("inline:"):
            status, value = self._request(credentials, "GET", path, params={"format": "full"})
            if status == 404:
                raise ProviderError("SOURCE_ATTACHMENT_REMOVED")
            part = value["payload"]
            for index in attachment.removeprefix("inline:").split(".")[1:]:
                part = part["parts"][int(index)]
            return decode(str(part["body"]["data"]))
        status, value = self._request(
            credentials, "GET", f"{path}/attachments/{quote(attachment, safe='')}"
        )
        if status == 404:
            raise ProviderError("SOURCE_ATTACHMENT_REMOVED")
        return decode(str(value["data"]))

    def subscribe(
        self,
        credentials: dict[str, Any],
        folder: str,
        callback: str,
        secret: str,
        previous: str | None,
    ) -> Subscription:
        if not self.settings.gmail_pubsub_topic:
            raise ProviderError("GMAIL_PUSH_NOT_CONFIGURED")
        _, result = self._request(
            credentials,
            "POST",
            "users/me/watch",
            payload={
                "topicName": self.settings.gmail_pubsub_topic,
                "labelIds": [folder],
                "labelFilterBehavior": "include",
            },
        )
        return Subscription(
            "gmail-watch", datetime.fromtimestamp(int(result["expiration"]) / 1000, UTC)
        )

    def disconnect(self, credentials: dict[str, Any], subscription: str | None) -> None:
        if subscription:
            self._request(credentials, "POST", "users/me/stop")
