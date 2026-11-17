"""Provider contracts run through local responses, never real provider accounts."""

from datetime import timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from recruitment.adapters import gmail, mail_http, microsoft
from recruitment.adapters.gmail import GmailMailboxProvider
from recruitment.adapters.microsoft import MicrosoftMailboxProvider
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.ports.mailbox import CursorExpired


def test_gmail_bootstrap_fetches_old_metadata_but_never_old_bodies(settings, monkeypatch):
    provider = GmailMailboxProvider(settings)
    cutoff = utcnow()
    calls = []

    def request(credentials, method, path, **kwargs):
        calls.append((path, kwargs))
        if path == "users/me/messages":
            return 200, {"messages": [{"id": "old"}, {"id": "new"}]}
        old = path.endswith("/old")
        if kwargs.get("params", {}).get("format") == "metadata":
            return 200, {
                "internalDate": str(
                    int((cutoff + timedelta(seconds=-1 if old else 1)).timestamp() * 1000)
                ),
                "labelIds": ["INBOX"],
            }
        assert not old, "Preconnection body must not be fetched."
        return 200, {
            "threadId": "conversation-1",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Candidate <candidate@example.org>"},
                    {"name": "Message-ID", "value": "<new@example.org>"},
                ],
                "parts": [
                    {
                        "filename": "cv.pdf",
                        "mimeType": "application/pdf",
                        "body": {"size": 15, "attachmentId": "cv"},
                    }
                ],
            },
        }

    monkeypatch.setattr(provider, "_request", request)
    page = provider.page({}, "INBOX", {"bootstrap": True, "history_id": "baseline"}, cutoff)
    assert [m.id for m in page.messages] == ["new"]
    assert page.messages[0].sender == "candidate@example.org"
    assert page.messages[0].thread_id == "conversation-1"
    assert page.cursor == {"history_id": "baseline"}
    assert sum(kwargs.get("params", {}).get("format") == "full" for _, kwargs in calls) == 1


def test_gmail_cursor_expiry_and_pkce(settings, monkeypatch):
    provider = GmailMailboxProvider(settings)
    captured = []
    flow = MagicMock()
    flow.authorization_url.return_value = (
        "https://accounts.google.com/local-authorization",
        "state",
    )

    def create(redirect, verifier, state):
        captured.append((redirect, verifier, state))
        return flow

    monkeypatch.setattr(provider, "_flow", create)
    auth = provider.authorize("bound-state", "https://recruitment.example.org/callback")
    assert len(auth.flow["verifier"]) >= 43
    assert captured[0][1] == auth.flow["verifier"]
    assert gmail.SCOPES == ["https://www.googleapis.com/auth/gmail.readonly"]
    monkeypatch.setattr(provider, "_request", lambda *args, **kwargs: (404, {}))
    with pytest.raises(CursorExpired):
        provider.page({}, "INBOX", {"history_id": "expired"}, utcnow())


def test_graph_metadata_cutoff_and_immutable_ids(settings, monkeypatch):
    provider = MicrosoftMailboxProvider(settings)
    cutoff = utcnow()
    calls = []

    def request(credentials, method, path, **kwargs):
        calls.append((path, kwargs))
        if "delta" in path:
            return 200, {
                "value": [
                    {"id": "old", "receivedDateTime": (cutoff - timedelta(days=1)).isoformat()},
                    {"id": "new", "receivedDateTime": (cutoff + timedelta(seconds=1)).isoformat()},
                ],
                "@odata.deltaLink": microsoft.ORIGIN
                + "/me/mailFolders/inbox/messages/delta?cursor=next",
            }
        assert "/old" not in path
        return 200, {
            "id": "new",
            "conversationId": "thread",
            "from": {"emailAddress": {"address": "candidate@example.org"}},
            "body": {"content": "A reply"},
            "internetMessageHeaders": [{"name": "Auto-Submitted", "value": "auto-replied"}],
        }

    monkeypatch.setattr(provider, "_request", request)
    baseline = provider.baseline({}, "inbox")
    assert "$filter" not in baseline["url"]
    page = provider.page({}, "inbox", baseline, cutoff)
    assert page.complete and len(page.messages) == 1
    assert page.messages[0].automated and page.messages[0].thread_id == "thread"
    observed = {}
    monkeypatch.setattr(provider, "_token", lambda credentials: "local-contract-value")

    def capture(*args, **kwargs):
        observed.update(kwargs)
        return 200, {}

    monkeypatch.setattr(microsoft, "provider_request", capture)
    MicrosoftMailboxProvider._request(provider, {}, "GET", "me")
    assert 'IdType="ImmutableId"' in observed["headers"]["Prefer"]
    assert set(microsoft.SCOPES) == {"Mail.Read", "User.Read"}


@pytest.mark.parametrize(
    "path",
    [
        "https://other.example.org/v1.0/me",
        "https://graph.microsoft.com.evil/v1.0/me",
        "https://user@graph.microsoft.com/v1.0/me",
        "https://graph.microsoft.com/beta/me",
    ],
)
def test_graph_cursor_cannot_forward_credentials_to_another_origin(path):
    with pytest.raises(ProviderError, match="INVALID_PROVIDER_URL"):
        mail_http.provider_request(microsoft.ORIGIN, "GET", path, "local-contract-value")


@pytest.mark.parametrize(
    "status,code,retry",
    [
        (401, "MAILBOX_REAUTHORIZATION_REQUIRED", False),
        (429, "MAILBOX_UNAVAILABLE", True),
        (503, "MAILBOX_UNAVAILABLE", True),
        (302, "MAILBOX_REQUEST_FAILED", False),
    ],
)
def test_http_failures_are_bounded_and_sanitized(monkeypatch, status, code, retry):
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, json={"error": "sensitive upstream text"})
    )
    monkeypatch.setattr(
        mail_http.httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs)
    )
    with pytest.raises(ProviderError) as failure:
        mail_http.provider_request(microsoft.ORIGIN, "GET", "me", "local-contract-value")
    assert failure.value.code == code and failure.value.retryable == retry
    assert "sensitive" not in str(failure.value)


def test_http_payload_limit(settings, monkeypatch):
    original = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 50))
    monkeypatch.setattr(
        mail_http.httpx, "Client", lambda **kwargs: original(transport=transport, **kwargs)
    )
    with pytest.raises(ProviderError, match="PROVIDER_RESPONSE_TOO_LARGE"):
        mail_http.provider_request(microsoft.ORIGIN, "GET", "me", "local-contract-value", limit=10)


def test_locked_http_transport_initializes_without_optional_network_backends():
    # Construct the real HTTP transport without issuing any external requests.
    with httpx.Client(trust_env=False, timeout=30) as client:
        assert not client.is_closed
