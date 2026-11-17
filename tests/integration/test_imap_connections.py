import base64
import json
import secrets
from datetime import timedelta
from typing import cast
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import func, select

from recruitment.adapters.db.models import Application, Candidate, CVUpload, Job
from recruitment.adapters.db.phase2 import Mailbox, MailboxCredential
from recruitment.adapters.mail_fake import FakeIMAPProvider, FakeMailboxProvider
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.ports.mailbox import MailboxGrant, RemoteAttachment, RemoteMessage
from tests.support import identity


def connect_imap(context):
    context.settings.enabled_mailbox_methods = "imap,gmail_oauth,microsoft_oauth"
    response = context.client.post(
        f"{context.base}/mailboxes/imap",
        headers=context.headers,
        json={
            "email": "test.mailbox@gmail.com",
            "app_password": "test" * 4,
            "opening_id": context.opening,
        },
    )
    assert response.status_code == 201, response.text
    mailbox = response.json()
    assert mailbox["status"] == "needs_configuration"
    response = context.client.patch(
        f"{context.base}/mailboxes/{mailbox['id']}",
        headers=context.headers,
        json={"expected_generation": mailbox["generation"], "activate": True, "folder_id": "INBOX"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_imap_uses_shared_pipeline_and_deduplicates_direct_upload(context, cv):
    upload = context.upload(cv).json()
    mailbox = connect_imap(context)
    provider = cast(FakeIMAPProvider, context.providers.mailboxes.imap)
    now = utcnow()
    attachment = RemoteAttachment("file", "cv.pdf", "application/pdf", len(cv))
    provider.messages = [
        RemoteMessage("old", now - timedelta(days=1), attachments=[attachment]),
        RemoteMessage("new", now + timedelta(seconds=1), attachments=[attachment]),
    ]
    provider.files[("new", "file")] = cv
    context.drain()
    intake = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()[
        "items"
    ]
    assert len(intake) == 1 and intake[0]["upload_id"] == upload["upload_id"]
    assert provider.downloads == [("new", "file")]
    result = context.result(upload["upload_id"])
    assert result["matched"]
    context.client.post(f"{context.base}/mailboxes/{mailbox['id']}/sync", headers=context.headers)
    context.drain()
    assert len(provider.downloads) == 1


def test_disabling_imap_blocks_api_scheduled_and_queued_work_preserves_candidates(context, cv):
    upload = context.upload(cv).json()
    context.drain()
    mailbox = connect_imap(context)
    provider = cast(FakeIMAPProvider, context.providers.mailboxes.imap)
    provider.messages = [
        RemoteMessage(
            "new",
            utcnow(),
            attachments=[RemoteAttachment("file", "cv.pdf", "application/pdf", len(cv))],
        )
    ]
    provider.files[("new", "file")] = cv
    context.settings.enabled_mailbox_methods = "gmail_oauth,microsoft_oauth"
    capabilities = context.client.get(
        f"{context.base}/mailbox-providers", headers=context.headers
    ).json()
    assert [x["connection_method"] for x in capabilities["items"]] == [
        "gmail_oauth",
        "microsoft_oauth",
    ]
    assert not context.client.get(
        f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers
    ).json()["enabled"]
    for suffix, method, body in [
        ("/sync", "POST", {}),
        ("/folders", "GET", None),
        ("", "PATCH", {"expected_generation": mailbox["generation"], "activate": True}),
        (
            "/imap-credentials",
            "POST",
            {"expected_generation": mailbox["generation"], "app_password": "test" * 4},
        ),
    ]:
        response = context.client.request(
            method,
            f"{context.base}/mailboxes/{mailbox['id']}{suffix}",
            headers=context.headers,
            json=body,
        )
        assert (
            response.status_code == 403
            and response.json()["error"]["code"] == "MAILBOX_METHOD_DISABLED"
        )
    context.drain()
    assert not provider.downloads
    assert context.result(upload["upload_id"])["matched"]
    assert (
        context.client.post(
            f"{context.base}/mailboxes/imap",
            headers=context.headers,
            json={
                "email": "other@gmail.com",
                "app_password": "test" * 4,
                "opening_id": context.opening,
            },
        ).status_code
        == 403
    )
    # Cleanup stays available even while the method is disabled.
    assert (
        context.client.delete(
            f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers
        ).status_code
        == 202
    )
    context.drain()
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(func.count()).select_from(MailboxCredential)) == 0
        for model in (Application, Candidate, CVUpload):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_imap_reauthorization_keeps_original_cutoff_and_cursor(context):
    mailbox = connect_imap(context)
    context.drain()
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        cursor = session.get(Mailbox, UUID(mailbox["id"])).cursor
    response = context.client.post(
        f"{context.base}/mailboxes/{mailbox['id']}/imap-credentials",
        headers=context.headers,
        json={"expected_generation": mailbox["generation"], "app_password": "test" * 4},
    )
    assert response.status_code == 200
    assert response.json()["intake_started_at"] == mailbox["intake_started_at"]
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.get(Mailbox, UUID(mailbox["id"])).cursor == cursor
    assert (
        context.client.post(
            f"{context.base}/mailboxes/{mailbox['id']}/imap-credentials",
            headers=context.headers,
            json={"expected_generation": mailbox["generation"], "app_password": "test" * 4},
        ).status_code
        == 409
    )


def test_imap_credentials_are_encrypted_and_never_returned(context, monkeypatch):
    context.settings.credential_keys = SecretStr(
        json.dumps({"1": base64.b64encode(secrets.token_bytes(32)).decode()})
    )
    context.settings.enabled_mailbox_methods = "imap"
    monkeypatch.setattr(
        context.providers.mailboxes.imap,
        "login",
        lambda email, password: MailboxGrant(
            email, email, {"email": email, "app_password": password}
        ),
    )
    response = context.client.post(
        f"{context.base}/mailboxes/imap",
        headers=context.headers,
        json={
            "email": "private@gmail.com",
            "app_password": "test" * 4,
            "opening_id": context.opening,
        },
    )
    assert response.status_code == 201
    assert "testtest" not in response.text and "app_password" not in response.text
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        row = session.scalar(select(MailboxCredential))
        assert (
            row.key_id == "1"
            and b"testtest" not in row.ciphertext
            and b"private@" not in row.ciphertext
        )
    invalid = context.client.post(
        f"{context.base}/mailboxes/imap",
        headers=context.headers,
        json={"email": "bad", "app_password": "secret", "opening_id": context.opening},
    )
    assert invalid.status_code == 422 and "secret" not in invalid.text


def test_imap_endpoints_enforce_auth_and_tenant_isolation(context):
    mailbox = connect_imap(context)
    _, stranger = identity(context.client)
    payload = {
        "email": "stranger@gmail.com",
        "app_password": "test" * 4,
        "opening_id": context.opening,
    }
    for headers, status in [({}, 401), (stranger, 404)]:
        assert (
            context.client.post(
                f"{context.base}/mailboxes/imap", headers=headers, json=payload
            ).status_code
            == status
        )
        assert (
            context.client.post(
                f"{context.base}/mailboxes/{mailbox['id']}/imap-credentials",
                headers=headers,
                json={"expected_generation": mailbox["generation"], "app_password": "test" * 4},
            ).status_code
            == status
        )


def test_switch_to_oauth_requires_disconnect_and_preserves_data(context, cv):
    context.upload(cv)
    context.drain()
    mailbox = connect_imap(context)
    gmail = cast(FakeMailboxProvider, context.providers.mailboxes.providers["gmail"])
    gmail.owner = "testmailbox+alias@gmail.com"

    def authorize():
        start = context.client.post(
            f"{context.base}/mailboxes",
            headers=context.headers,
            json={"provider": "gmail", "opening_id": context.opening},
        ).json()
        state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
        return context.client.post(
            "/api/v1/mailbox-oauth/gmail/callback",
            headers=context.headers,
            json={"state": state, "code": "local-fake"},
        )

    assert authorize().status_code == 409
    context.settings.enabled_mailbox_methods = "gmail_oauth,microsoft_oauth"
    context.client.delete(f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers)
    context.drain()
    result = authorize()
    assert result.status_code == 200 and result.json()["connection_method"] == "gmail_oauth"
    assert (
        len(
            context.client.get(f"{context.base}/candidates", headers=context.headers).json()[
                "items"
            ]
        )
        == 1
    )


def test_imap_scheduler_uses_poll_interval_and_skips_disabled_connections(context):
    mailbox = connect_imap(context)
    context.drain()
    db = context.client.app.state.container.db
    tenant, user = UUID(context.tenant), UUID(context.user_id)
    with db.transaction(db.api, tenant, user) as session:
        session.get(Mailbox, UUID(mailbox["id"])).next_sync_at = utcnow() - timedelta(minutes=1)
    before = utcnow()
    context.worker().queue.dispatch()
    with db.transaction(db.api, tenant, user) as session:
        row = session.get(Mailbox, UUID(mailbox["id"]))
        assert 55 <= (row.next_sync_at - before).total_seconds() <= 65
        assert row.next_maintenance_at is None
        row.next_sync_at = due = utcnow() - timedelta(minutes=1)
    context.settings.enabled_mailbox_methods = "gmail_oauth,microsoft_oauth"
    context.worker().queue.dispatch()
    with db.transaction(db.api, tenant, user) as session:
        assert session.get(Mailbox, UUID(mailbox["id"])).next_sync_at == due


def test_imap_worker_retries_transient_failure_without_rejecting_any_candidate(
    context, cv, monkeypatch
):
    upload = context.upload(cv).json()
    context.drain()
    mailbox = connect_imap(context)
    provider = context.providers.mailboxes.imap
    original = provider.page

    def unavailable(*args):
        raise ProviderError("MAILBOX_UNAVAILABLE", retryable=True)

    monkeypatch.setattr(provider, "page", unavailable)
    context.drain()
    db = context.client.app.state.container.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        jobs = list(session.scalars(select(Job).where(Job.aggregate_id == UUID(mailbox["id"]))))
        assert jobs and all(job.state == "retry_wait" for job in jobs)
        for job in jobs:
            job.available_at = utcnow()
    monkeypatch.setattr(provider, "page", original)
    context.drain()
    row = context.client.get(
        f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers
    ).json()
    assert row["sync_status"] == "idle" and row["last_success_at"]
    assert context.result(upload["upload_id"])["matched"]
