import base64
import json
from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import func, select

from recruitment.adapters.db.phase2 import (
    MailboxCredential,
    MailboxReceipt,
)
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.ports.mailbox import RemoteAttachment, RemoteMessage, Sealed
from tests.integration.test_mail_intake import connect, fake, new_mail, sync
from tests.support import pdf_bytes


def contacts(context):
    return context.client.get(f"{context.base}/candidates", headers=context.headers).json()["items"]


def test_late_old_cv_cannot_replace_newer_profile_or_match(context, cv):
    older = context.upload(cv).json()["upload_id"]
    worker = context.worker()
    worker.queue.dispatch()
    lease = worker.queue.claim()
    assert lease
    profile = worker.processor._parse(lease)
    assert worker.processor._resolve(lease, profile) is not None
    newer = context.upload(
        pdf_bytes(
            "Updated Candidate\ncandidate@example.org\n+919876543210\n"
            "Python development with recent evidence."
        )
    ).json()["upload_id"]
    context.drain()
    current = context.result(newer)
    worker.processor.process(lease)
    worker.queue.finish(lease)
    app = context.client.get(
        f"{context.base}/applications/{current['application_id']}/match-results",
        headers=context.headers,
    ).json()
    assert app["evaluation"]["id"] == current["evaluation_id"]
    assert contacts(context)[0]["profile"]["name"] == "Updated Candidate"
    old_status = context.client.get(
        f"{context.base}/cv-uploads/{older}", headers=context.headers
    ).json()
    assert old_status["review_reason"] == "SUPERSEDED_CV"


def test_contact_corrections_are_audited_versioned_and_never_steal_keys(context, cv):
    context.upload(cv)
    context.drain()
    first = contacts(context)[0]
    context.upload(
        pdf_bytes("Second Person\nsecond@example.org\n+919876543211\nPython development.")
    )
    context.drain()
    path = f"{context.base}/candidates/{first['id']}"
    payload = {
        "expected_revision": first["contact_revision"],
        "reason": "Verified the candidate contact in person.",
        "email": "Corrected@Example.org",
    }
    corrected = context.client.patch(path, headers=context.headers, json=payload)
    assert corrected.status_code == 200, corrected.text
    assert corrected.json()["profile"]["email"] == "corrected@example.org"
    assert corrected.json()["contact_revision"] == first["contact_revision"] + 1
    assert context.client.patch(path, headers=context.headers, json=payload).status_code == 409
    clash = {
        **payload,
        "expected_revision": corrected.json()["contact_revision"],
        "email": "second@example.org",
    }
    assert (
        context.client.patch(path, headers=context.headers, json=clash).json()["error"]["code"]
        == "CONTACT_CONFLICT"
    )
    history = context.client.get(path, headers=context.headers).json()["contact_history"]
    assert len(history) == 1 and history[0]["after"]["email"] == "corrected@example.org"
    # A subsequent CV with the original identity cannot overwrite the audited correction.
    context.upload(
        pdf_bytes(
            "Updated Candidate\ncandidate@example.org\n+919876543210\n"
            "Python development and PostgreSQL."
        )
    )
    context.drain()
    assert (
        context.client.get(path, headers=context.headers).json()["profile"]["email"]
        == "corrected@example.org"
    )


@pytest.mark.parametrize("decision", ["link", "create"])
def test_identity_conflict_resolution_is_explicit_idempotent_and_preserves_owned_contacts(
    context, cv, decision
):
    context.upload(cv)
    context.upload(
        pdf_bytes("Second Person\nsecond@example.org\n+919876543211\nPython development.")
    )
    context.drain()
    before = {item["id"]: item for item in contacts(context)}
    first = next(
        item for item in before.values() if item["profile"]["email"] == "candidate@example.org"
    )
    changed = context.upload(
        pdf_bytes("Conflicted Person\ncandidate@example.org\n+919876543211\nPython development.")
    ).json()["upload_id"]
    context.drain()
    cases = context.client.get(f"{context.base}/identity-reviews", headers=context.headers).json()[
        "items"
    ]
    assert (
        len(cases) == 1
        and cases[0]["upload_id"] == changed
        and len(cases[0]["candidate_options"]) == 2
    )
    path = f"{context.base}/identity-reviews/{cases[0]['id']}/resolve"
    payload = {"decision": decision, "reason": "Recruiter verified the person who owns this CV."}
    if decision == "link":
        payload["candidate_id"] = first["id"]
    headers = {**context.headers, "Idempotency-Key": str(uuid4())}
    for _ in range(2):
        response = context.client.post(path, headers=headers, json=payload)
        assert response.status_code == 202, response.text
    context.drain()
    current = contacts(context)
    assert len(current) == (3 if decision == "create" else 2)
    for item in current:
        if item["id"] in before:
            assert item["profile"]["email"] == before[item["id"]]["profile"]["email"]
            assert item["profile"]["phone"] == before[item["id"]]["profile"]["phone"]
        else:
            assert item["profile"]["email"] == "" and item["profile"]["phone"] == ""
    result = context.result(changed)
    assert result["recommendation"] == (
        "needs_review" if decision == "link" else "recommended_shortlist"
    )
    assert (
        context.client.get(
            f"{context.base}/applications/{result['application_id']}", headers=context.headers
        ).json()["status"]
        != "rejected"
    )


def test_self_sent_and_auto_replies_are_ignored_and_correlated_contacts_are_proposals(context, cv):
    mailbox = connect(context)
    initial = replace(new_mail(context, cv), thread_id="candidate-thread")
    provider = fake(context)
    provider.messages[-1] = initial
    context.drain()
    original = contacts(context)[0]
    for message in (
        replace(initial, id="self", sender=mailbox["email"]),
        replace(initial, id="automatic", automated=True),
        RemoteMessage(
            "contact-reply",
            utcnow(),
            thread_id="candidate-thread",
            reply_text="My new email is corrected@example.org",
        ),
        RemoteMessage(
            "bounce",
            utcnow(),
            in_reply_to=initial.internet_message_id,
            automated=True,
            bounce=True,
            attachments=[RemoteAttachment("original", "cv.pdf", "application/pdf", len(cv))],
        ),
        RemoteMessage("unrelated", utcnow(), reply_text="another@example.org"),
    ):
        provider.messages.append(message)
    sync(context, mailbox)
    cases = context.client.get(f"{context.base}/identity-reviews", headers=context.headers).json()[
        "items"
    ]
    assert {case["kind"] for case in cases} == {"contact_proposal", "contact_issue"}
    assert len(provider.downloads) == 1 and len(contacts(context)) == 1
    assert contacts(context)[0]["profile"]["email"] == original["profile"]["email"]
    proposed = next(case for case in cases if case["kind"] == "contact_proposal")
    accepted = context.client.post(
        f"{context.base}/identity-reviews/{proposed['id']}/resolve",
        headers={**context.headers, "Idempotency-Key": str(uuid4())},
        json={
            "decision": "accept",
            "reason": "Confirmed the candidate reply by phone.",
            "expected_revision": original["contact_revision"],
        },
    )
    assert accepted.status_code == 202, accepted.text
    assert contacts(context)[0]["profile"]["email"] == "corrected@example.org"


def test_graph_notifications_are_verified_before_queueing_and_replays_are_deduplicated(context):
    mailbox = connect(context, "microsoft")
    context.drain()
    db = context.client.app.state.container.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        credential = session.scalar(select(MailboxCredential))
        runtime = context.providers.mailboxes
        value = runtime.vault.open(
            Sealed(credential.key_id, credential.nonce, credential.ciphertext),
            f"{context.tenant}:{mailbox['id']}:mailbox",
        )
        secret = value["notification_secret"]
    path = f"/api/v1/mailbox-webhooks/microsoft/{mailbox['id']}"
    payload = {
        "value": [
            {
                "clientState": secret,
                "subscriptionId": "fake-subscription",
                "changeType": "created",
                "resource": "me/messages/new",
                "resourceData": {"id": "new"},
            }
        ]
    }
    # Obtain the actual subscription identifier, with no provider credentials exposed.
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        from recruitment.adapters.db.phase2 import Mailbox

        payload["value"][0]["subscriptionId"] = session.get(
            Mailbox, UUID(mailbox["id"])
        ).subscription_id
    forged = json.loads(json.dumps(payload))
    forged["value"][0]["clientState"] = "forged"
    assert context.client.post(path, json=forged).status_code == 401
    for _ in range(2):
        assert context.client.post(path, json=payload).status_code == 202
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        assert session.scalar(select(func.count()).select_from(MailboxReceipt)) == 1
    context.drain()
    assert (
        context.client.post(path + "?validationToken=provider-check", content=b"").text
        == "provider-check"
    )


def test_gmail_forged_push_and_expired_oauth_cannot_queue_work(context, admin_url):
    start = context.client.post(
        f"{context.base}/mailboxes",
        headers=context.headers,
        json={"provider": "gmail", "opening_id": context.opening},
    ).json()
    from urllib.parse import parse_qs, urlsplit

    state = parse_qs(urlsplit(start["authorization_url"]).query)["state"][0]
    with psycopg.connect(admin_url) as connection:
        connection.execute("UPDATE mailbox_oauth_attempts SET expires_at=now()-interval '1 second'")
    expired = context.client.post(
        "/api/v1/mailbox-oauth/gmail/callback",
        headers=context.headers,
        json={"state": state, "code": "local-fake"},
    )
    assert expired.status_code == 410
    push = {
        "message": {
            "messageId": "forged",
            "data": base64.b64encode(
                json.dumps({"emailAddress": "someone@example.org"}).encode()
            ).decode(),
        }
    }
    assert (
        context.client.post(
            "/api/v1/mailbox-webhooks/gmail", json=push, headers={"Authorization": "Bearer forged"}
        ).status_code
        == 401
    )


def test_disconnect_fences_inflight_attachment_before_original_is_stored(context, cv, monkeypatch):
    mailbox = connect(context)
    new_mail(context, cv)
    provider = fake(context)
    original = provider.attachment

    def disconnect(*args):
        content = original(*args)
        assert (
            context.client.delete(
                f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers
            ).status_code
            == 202
        )
        return content

    monkeypatch.setattr(provider, "attachment", disconnect)
    context.drain()
    assert contacts(context) == []
    assert (
        context.client.get(f"{context.base}/cv-uploads", headers=context.headers).json()["items"]
        == []
    )


def test_retention_removes_mail_metadata_contacts_and_converted_documents(context, cv, admin_url):
    from recruitment.adapters.db.models import Candidate, CVUpload, Tombstone
    from recruitment.adapters.db.phase2 import (
        ContactRevision,
        DocumentDerivative,
        IdentityReview,
        MailAttachment,
        MailMessage,
    )

    mailbox = connect(context)
    new_mail(context, cv)
    context.drain()
    candidate = contacts(context)[0]
    corrected = context.client.patch(
        f"{context.base}/candidates/{candidate['id']}",
        headers=context.headers,
        json={
            "expected_revision": candidate["contact_revision"],
            "name": "Reviewed Candidate",
            "reason": "Recruiter verified the name in the CV.",
        },
    )
    assert corrected.status_code == 200
    upload = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()[
        "items"
    ][0]
    db = context.client.app.state.container.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        session.add(
            DocumentDerivative(
                tenant_id=UUID(context.tenant),
                upload_id=UUID(upload["upload_id"]),
                content=cv,
                sha256="retention-fixture",
                converter_version="local-fixture",
            )
        )
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "UPDATE applications SET last_activity_at=now()-interval '40 days',"
            "retention_expires_at=now()-interval '1 day' WHERE id=%s",
            (upload["application_id"],),
        )
    context.settings.auto_delete_enabled = True
    context.drain()
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        for model in (
            Candidate,
            ContactRevision,
            DocumentDerivative,
            MailMessage,
            MailAttachment,
            IdentityReview,
            CVUpload,
        ):
            assert session.scalar(select(func.count()).select_from(model)) == 0, model.__tablename__
        assert session.scalar(select(func.count()).select_from(Tombstone)) == 1
    sync(context, mailbox)
    assert len(fake(context).downloads) == 1


def test_expired_mail_credentials_require_reauthorization_without_advancing_cursor(
    context, monkeypatch
):
    mailbox = connect(context)
    context.drain()

    def expired(*args):
        raise ProviderError("MAILBOX_REAUTHORIZATION_REQUIRED")

    monkeypatch.setattr(fake(context), "page", expired)
    sync(context, mailbox)
    state = context.client.get(
        f"{context.base}/mailboxes/{mailbox['id']}", headers=context.headers
    ).json()
    assert state["status"] == "reauthorization_required"
    assert state["intake_started_at"] == mailbox["intake_started_at"]
    assert state["failure_code"] == "MAILBOX_REAUTHORIZATION_REQUIRED"


def test_public_phase2_schemas_describe_resources_and_safe_errors(context):
    schema = context.client.get("/openapi.json").json()
    for name in ("MailboxRead", "CandidateRead", "IntakeRead", "ResumeVersionRead", "OverviewRead"):
        assert name in schema["components"]["schemas"]
    assert (
        context.client.get(f"{context.base}/mailbox-providers", headers=context.headers).status_code
        == 200
    )
    assert (
        context.client.get(f"{context.base}/overview?days=1", headers=context.headers).status_code
        == 200
    )
