from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import func, select

from recruitment.adapters.db.models import (
    Application,
    Candidate,
    CVUpload,
    ResumeChunk,
    ResumeFile,
)
from recruitment.adapters.db.phase2 import (
    MailAttachment,
    Mailbox,
    MailboxCredential,
    MailMessage,
    ProcessingSnapshot,
)
from recruitment.adapters.mail_fake import FakeMailboxProvider
from recruitment.core.time import utcnow
from recruitment.ports.mailbox import CursorExpired, MessagePage, RemoteAttachment, RemoteMessage
from tests.support import Context, identity, pdf_bytes


def authorize(context: Context, provider: str = "gmail") -> dict:
    start = context.client.post(
        f"{context.base}/mailboxes",
        headers=context.headers,
        json={"provider": provider, "opening_id": context.opening},
    )
    assert start.status_code == 201, start.text
    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    completed = context.client.post(
        f"/api/v1/mailbox-oauth/{provider}/callback",
        headers=context.headers,
        json={"state": state, "code": "local-fake"},
    )
    assert completed.status_code == 200, completed.text
    return completed.json()


def connect(context: Context, provider: str = "gmail") -> dict:
    row = authorize(context, provider)
    activated = context.client.patch(
        f"{context.base}/mailboxes/{row['id']}",
        headers=context.headers,
        json={"expected_generation": row["generation"], "activate": True},
    )
    assert activated.status_code == 200, activated.text
    return activated.json()


def fake(context: Context, provider: str = "gmail") -> FakeMailboxProvider:
    return context.providers.mailboxes.providers[provider]


def new_mail(
    context: Context, content: bytes, when: datetime | None = None, provider: str = "gmail"
) -> RemoteMessage:
    identifier = str(uuid4())
    message = RemoteMessage(
        identifier,
        when or utcnow(),
        f"<{identifier}@example.org>",
        [RemoteAttachment("cv", "candidate.pdf", "application/pdf", len(content))],
    )
    fake(context, provider).messages.append(message)
    fake(context, provider).files[(identifier, "cv")] = content
    return message


def sync(context: Context, mailbox: dict) -> None:
    response = context.client.post(
        f"{context.base}/mailboxes/{mailbox['id']}/sync", headers=context.headers
    )
    assert response.status_code == 202
    context.drain()


@pytest.mark.parametrize("provider", ["gmail", "microsoft"])
def test_only_new_mail_enters_the_complete_cv_pipeline(
    context: Context, cv: bytes, provider: str
) -> None:
    old = new_mail(context, cv, utcnow() - timedelta(days=2), provider)
    mailbox = connect(context, provider)
    current = new_mail(context, cv, provider=provider)
    context.drain()
    assert fake(context, provider).downloads == [(current.id, "cv")]
    intake = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()[
        "items"
    ]
    assert len(intake) == 1 and intake[0]["status"] == "completed", intake
    result = context.result(intake[0]["upload_id"])
    assert result["score"] == 100 and result["criteria"][0]["evidence"]
    assert result["embedding_fingerprint"] != "legacy-unfingerprinted"
    assert result["processing_config"]["actual_embedding_model"] == "fake-embedding-v1"
    candidates = context.client.get(f"{context.base}/candidates", headers=context.headers).json()[
        "items"
    ]
    assert len(candidates) == 1 and candidates[0]["profile"]["email"] == "candidate@example.org"
    assert candidates[0]["profile"]["email"] != mailbox["email"]
    original = context.client.get(
        f"{context.base}/cv-uploads/{intake[0]['upload_id']}/file", headers=context.headers
    )
    assert original.content == cv
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(MailMessage.provider_message_id)) != old.id
        assert session.scalar(select(ResumeFile.content)) == cv
        assert (
            session.scalar(select(ResumeChunk.embedding_fingerprint))
            == result["embedding_fingerprint"]
        )
        assert (
            session.scalar(select(ProcessingSnapshot.fingerprint))
            == result["embedding_fingerprint"]
        )


def test_provider_returning_old_bootstrap_items_cannot_bypass_cutoff(
    context: Context, cv: bytes, monkeypatch
) -> None:
    old = new_mail(context, cv, utcnow() - timedelta(hours=1))
    connect(context)
    provider = fake(context)
    monkeypatch.setattr(provider, "page", lambda *args: MessagePage([old], {"offset": 1}, True))
    context.drain()
    assert provider.downloads == []
    assert (
        context.client.get(f"{context.base}/candidates", headers=context.headers).json()["items"]
        == []
    )
    assert (
        context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()["items"]
        == []
    )


def test_candidate_contact_is_never_filled_from_mailbox(context: Context) -> None:
    connect(context)
    new_mail(context, pdf_bytes("Nameless Contact Candidate\nPython development for three years."))
    context.drain()
    candidate = context.client.get(f"{context.base}/candidates", headers=context.headers).json()[
        "items"
    ][0]
    assert candidate["profile"]["email"] is None
    assert candidate["profile"]["mobile"] is None


def test_email_and_upload_duplicates_share_one_application(context: Context, cv: bytes) -> None:
    direct = context.upload(cv).json()
    connect(context)
    new_mail(context, cv)
    context.drain()
    intake = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()[
        "items"
    ]
    assert intake[0]["upload_id"] == direct["upload_id"]
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        for model in (Application, Candidate, ResumeFile, CVUpload):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_reauthorization_keeps_cutoff_and_cursor(context: Context, cv: bytes) -> None:
    mailbox = connect(context)
    new_mail(context, cv)
    context.drain()
    row = context.client.get(
        f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers
    ).json()
    requested = context.client.patch(
        f"{context.base}/mailboxes/{row['id']}",
        headers=context.headers,
        json={"expected_generation": row["generation"], "reauthorize": True},
    ).json()
    state = parse_qs(urlsplit(requested["authorization_url"]).query)["state"][0]
    reconnected = context.client.post(
        "/api/v1/mailbox-oauth/gmail/callback",
        headers=context.headers,
        json={"state": state, "code": "local-fake"},
    )
    assert reconnected.status_code == 200
    assert reconnected.json()["intake_started_at"] == mailbox["intake_started_at"]
    context.drain()
    assert len(fake(context).downloads) == 1


def test_folder_change_discards_history_and_fences_queued_attachments(
    context: Context, cv: bytes
) -> None:
    mailbox = connect(context)
    new_mail(context, cv)
    worker = context.worker()
    worker.queue.dispatch()
    for _ in range(4):
        worker.run_once()
        intake = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()[
            "items"
        ]
        if intake:
            break
    changed = context.client.patch(
        f"{context.base}/mailboxes/{mailbox['id']}",
        headers=context.headers,
        json={
            "expected_generation": mailbox["generation"],
            "folder_id": "cv-folder",
            "activate": True,
        },
    )
    assert changed.status_code == 200
    assert changed.json()["intake_started_at"] >= mailbox["intake_started_at"]
    context.drain()
    assert (
        context.client.get(f"{context.base}/candidates", headers=context.headers).json()["items"]
        == []
    )


def test_expired_cursor_recovery_does_not_reimport_deleted_data(
    context: Context, cv: bytes, admin_url: str, monkeypatch
) -> None:
    mailbox = connect(context)
    message = new_mail(context, cv)
    context.drain()
    upload = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()[
        "items"
    ][0]
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "UPDATE applications SET last_activity_at=now()-interval '40 days',"
            "retention_expires_at=now()-interval '1 day' WHERE id=%s",
            (upload["application_id"],),
        )
    context.settings.auto_delete_enabled = True
    context.drain()
    assert (
        context.client.get(f"{context.base}/candidates", headers=context.headers).json()["items"]
        == []
    )
    provider = fake(context)
    original = provider.page
    calls = 0

    def recovering(credentials, folder, cursor, after):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CursorExpired
        return MessagePage([message], {"offset": len(provider.messages)}, True)

    monkeypatch.setattr(provider, "page", recovering)
    sync(context, mailbox)
    assert calls >= 2
    assert len(provider.downloads) == 1
    assert (
        context.client.get(f"{context.base}/candidates", headers=context.headers).json()["items"]
        == []
    )
    monkeypatch.setattr(provider, "page", original)


def test_oauth_is_actor_bound_single_use_and_does_not_expose_credentials(context: Context) -> None:
    start = context.client.post(
        f"{context.base}/mailboxes",
        headers=context.headers,
        json={"provider": "gmail", "opening_id": context.opening},
    ).json()
    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    _, stranger = identity(context.client)
    payload = {"state": state, "code": "local-fake"}
    assert (
        context.client.post(
            "/api/v1/mailbox-oauth/gmail/callback", headers=stranger, json=payload
        ).status_code
        == 410
    )
    completed = context.client.post(
        "/api/v1/mailbox-oauth/gmail/callback", headers=context.headers, json=payload
    )
    assert completed.status_code == 200
    assert (
        context.client.post(
            "/api/v1/mailbox-oauth/gmail/callback", headers=context.headers, json=payload
        ).status_code
        == 410
    )
    assert not any(key in completed.text for key in ('"ciphertext"', '"refresh_token"', '"nonce"'))


def test_disconnect_stops_intake_and_removes_grants(context: Context, cv: bytes) -> None:
    mailbox = connect(context)
    new_mail(context, cv)
    assert (
        context.client.delete(
            f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers
        ).status_code
        == 202
    )
    context.drain()
    assert fake(context).downloads == []
    row = context.client.get(
        f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers
    ).json()
    assert row["status"] == "disconnected"
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(func.count()).select_from(MailboxCredential)) == 0


def test_mailbox_candidate_and_download_endpoints_enforce_membership(
    context: Context, cv: bytes
) -> None:
    mailbox = connect(context)
    new_mail(context, cv)
    context.drain()
    candidate = context.client.get(f"{context.base}/candidates", headers=context.headers).json()[
        "items"
    ][0]
    upload = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()[
        "items"
    ][0]
    stranger_id, stranger = identity(context.client)
    for path in (
        "mailboxes",
        f"mailboxes/{mailbox['id']}",
        "mail-intake",
        "candidates",
        f"candidates/{candidate['id']}",
        f"candidates/{candidate['id']}/resumes",
        f"cv-uploads/{upload['upload_id']}/file",
        "identity-reviews",
    ):
        assert context.client.get(f"{context.base}/{path}", headers=stranger).status_code == 404
        assert context.client.get(f"{context.base}/{path}").status_code == 401
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(stranger_id)) as session:
        for model in (Mailbox, MailAttachment, MailMessage, Candidate, ResumeFile):
            assert session.scalars(select(model)).all() == []
