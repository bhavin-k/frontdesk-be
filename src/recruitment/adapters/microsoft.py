from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any
from urllib.parse import quote

import msal

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

ORIGIN = "https://graph.microsoft.com/v1.0"
SCOPES = ["Mail.Read", "User.Read"]
HEADERS = {
    "Prefer": 'IdType="ImmutableId", outlook.body-content-type="text", odata.maxpagesize=100'
}


class MicrosoftMailboxProvider:
    name = "microsoft"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _client(self, serialized: str | None = None) -> tuple[Any, Any]:
        if not self.settings.microsoft_client_id or not self.settings.microsoft_client_secret:
            raise ProviderError("MICROSOFT_NOT_CONFIGURED")
        cache = msal.SerializableTokenCache()
        if serialized:
            cache.deserialize(serialized)
        client = msal.ConfidentialClientApplication(
            self.settings.microsoft_client_id,
            authority=self.settings.microsoft_authority,
            client_credential=self.settings.microsoft_client_secret.get_secret_value(),
            token_cache=cache,
            timeout=30,
            enable_pii_log=False,
        )
        return client, cache

    def authorize(self, state: str, redirect_uri: str) -> AuthorizationStart:
        client, _ = self._client()
        flow = client.initiate_auth_code_flow(
            SCOPES, redirect_uri=redirect_uri, state=state, response_mode="query"
        )
        if "auth_uri" not in flow:
            raise ProviderError("MICROSOFT_AUTHORIZATION_FAILED")
        return AuthorizationStart(str(flow["auth_uri"]), dict(flow))

    def exchange(self, flow: dict[str, Any], code: str, redirect_uri: str) -> MailboxGrant:
        client, cache = self._client()
        try:
            result = client.acquire_token_by_auth_code_flow(
                flow, {"state": flow["state"], "code": code}
            )
            if not result.get("access_token"):
                raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED")
            _, profile = provider_request(
                ORIGIN,
                "GET",
                "me",
                str(result["access_token"]),
                params={"$select": "id,mail,userPrincipalName"},
                headers=HEADERS,
            )
            accounts = client.get_accounts()
            if len(accounts) != 1:
                raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED")
            return MailboxGrant(
                str(profile["id"]),
                str(profile.get("mail") or profile["userPrincipalName"]).casefold(),
                {"cache": cache.serialize(), "account": accounts[0]["home_account_id"]},
            )
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED") from exc

    def _token(self, credentials: dict[str, Any]) -> str:
        client, cache = self._client(credentials.get("cache"))
        accounts = [
            a for a in client.get_accounts() if a["home_account_id"] == credentials.get("account")
        ]
        if not accounts:
            raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED")
        result = client.acquire_token_silent(SCOPES, account=accounts[0])
        credentials["cache"] = cache.serialize()
        if not result or not result.get("access_token"):
            raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED")
        return str(result["access_token"])

    def _request(
        self, credentials: dict[str, Any], method: str, path: str, **kwargs: Any
    ) -> tuple[int, Any]:
        return provider_request(
            ORIGIN, method, path, self._token(credentials), headers=HEADERS, **kwargs
        )

    def folders(self, credentials: dict[str, Any]) -> list[dict[str, str]]:
        folders: list[dict[str, str]] = []
        pending = ["me/mailFolders?$select=id,displayName,childFolderCount&$top=100"]
        while pending:
            _, page = self._request(credentials, "GET", pending.pop(0))
            for row in page.get("value", []):
                folders.append({"id": str(row["id"]), "name": str(row["displayName"])})
                if row.get("childFolderCount"):
                    pending.append(
                        f"me/mailFolders/{quote(str(row['id']), safe='')}/childFolders"
                        "?$select=id,displayName,childFolderCount&$top=100"
                    )
            if len(folders) > 1000:
                raise ProviderError("MAILBOX_FOLDER_LIMIT")
            if page.get("@odata.nextLink"):
                pending.append(str(page["@odata.nextLink"]))
        return folders

    def baseline(self, credentials: dict[str, Any], folder: str) -> dict[str, Any]:
        # Metadata-only bootstrap avoids Graph's 5,000-item receivedDateTime filter cap.
        return {
            "url": f"{ORIGIN}/me/mailFolders/{quote(folder, safe='')}/messages/delta?"
            "$select=id,receivedDateTime,hasAttachments,internetMessageId&$top=100"
        }

    def _message(
        self, credentials: dict[str, Any], item: dict[str, Any], after: datetime
    ) -> RemoteMessage | None:
        if "@removed" in item or not item.get("receivedDateTime"):
            return None
        received = datetime.fromisoformat(str(item["receivedDateTime"]).replace("Z", "+00:00"))
        if received < after:
            return None
        identifier = str(item["id"])
        path = f"me/messages/{quote(identifier, safe='')}"
        status, message = self._request(
            credentials,
            "GET",
            path,
            params={
                "$select": (
                    "id,internetMessageId,internetMessageHeaders,body,hasAttachments,from,"
                    "conversationId"
                )
            },
        )
        if status == 404:
            return None
        headers = {
            str(h["name"]).casefold(): str(h["value"])
            for h in message.get("internetMessageHeaders", [])
        }
        attachments: list[RemoteAttachment] = []
        if message.get("hasAttachments"):
            next_url: str | None = (
                f"{path}/attachments?$select=id,name,contentType,size,isInline&$top=100"
            )
            while next_url:
                _, page = self._request(credentials, "GET", next_url)
                for row in page.get("value", []):
                    if (
                        row.get("@odata.type", "#microsoft.graph.fileAttachment")
                        == "#microsoft.graph.fileAttachment"
                    ):
                        attachments.append(
                            RemoteAttachment(
                                str(row["id"]),
                                str(row.get("name", "")),
                                str(row.get("contentType", "")),
                                int(row.get("size", 0)),
                                inline=row.get("isInline") is True,
                            )
                        )
                if len(attachments) > 100:
                    raise ProviderError("MESSAGE_ATTACHMENT_LIMIT")
                next_url = page.get("@odata.nextLink")
        return RemoteMessage(
            identifier,
            received,
            message.get("internetMessageId"),
            attachments,
            headers.get("in-reply-to"),
            str(message.get("body", {}).get("content", ""))[:65536],
            "delivery-status" in headers.get("content-type", "").casefold(),
            sender=parseaddr(
                str(message.get("from", {}).get("emailAddress", {}).get("address", ""))
            )[1].casefold(),
            automated=headers.get("auto-submitted", "no").casefold() != "no"
            or headers.get("precedence", "").casefold() in {"bulk", "list", "junk"},
            thread_id=message.get("conversationId"),
        )

    def page(
        self, credentials: dict[str, Any], folder: str, cursor: dict[str, Any], after: datetime
    ) -> MessagePage:
        status, page = self._request(credentials, "GET", str(cursor["url"]))
        if status in (404, 410):
            raise CursorExpired
        messages = [
            message
            for item in page.get("value", [])
            if (message := self._message(credentials, item, after)) is not None
        ]
        next_link = page.get("@odata.nextLink") or page.get("@odata.deltaLink")
        if not next_link:
            raise ProviderError("INVALID_MAILBOX_CURSOR")
        return MessagePage(messages, {"url": str(next_link)}, "@odata.deltaLink" in page)

    def attachment(self, credentials: dict[str, Any], message: str, attachment: str) -> bytes:
        status, content = self._request(
            credentials,
            "GET",
            f"me/messages/{quote(message, safe='')}/attachments/"
            f"{quote(attachment, safe='')}/$value",
            binary=True,
            limit=10 * 1024 * 1024,
        )
        if status == 404:
            raise ProviderError("SOURCE_ATTACHMENT_REMOVED")
        return bytes(content)

    def subscribe(
        self,
        credentials: dict[str, Any],
        folder: str,
        callback: str,
        secret: str,
        previous: str | None,
    ) -> Subscription:
        expiry = (utcnow() + timedelta(days=3)).astimezone(UTC)
        if previous:
            status, value = self._request(
                credentials,
                "PATCH",
                f"subscriptions/{quote(previous, safe='')}",
                payload={"expirationDateTime": expiry.isoformat()},
            )
            if status not in (404, 410):
                return Subscription(
                    str(value["id"]),
                    datetime.fromisoformat(str(value["expirationDateTime"]).replace("Z", "+00:00")),
                )
        _, result = self._request(
            credentials,
            "POST",
            "subscriptions",
            payload={
                "changeType": "created",
                "notificationUrl": callback,
                "resource": f"me/mailFolders/{quote(folder, safe='')}/messages",
                "expirationDateTime": expiry.isoformat(),
                "clientState": secret,
                "latestSupportedTlsVersion": "v1_2",
            },
        )
        return Subscription(
            str(result["id"]),
            datetime.fromisoformat(str(result["expirationDateTime"]).replace("Z", "+00:00")),
        )

    def disconnect(self, credentials: dict[str, Any], subscription: str | None) -> None:
        if subscription:
            self._request(credentials, "DELETE", f"subscriptions/{quote(subscription, safe='')}")
