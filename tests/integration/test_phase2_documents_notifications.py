import base64
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

from recruitment.adapters.db.phase2 import DocumentDerivative, MailboxReceipt
from recruitment.adapters.documents import DOC
from recruitment.core.errors import ProviderError
from tests.integration.test_mail_intake import connect

DOC_CONTENT = (Path(__file__).parents[1] / "fixtures" / "english-cv.doc").read_bytes()


def test_legacy_doc_stores_original_and_converted_pdf_with_fake_provenance(context):
    uploaded = context.upload(DOC_CONTENT, DOC)
    assert uploaded.status_code == 202, uploaded.text
    context.drain()
    result = context.result(uploaded.json()["upload_id"])
    assert result["matched"]
    original = context.client.get(
        f"{context.base}/cv-uploads/{uploaded.json()['upload_id']}/file",
        headers=context.headers,
    )
    assert original.content == DOC_CONTENT and original.headers["content-type"] == DOC
    db = context.client.app.state.container.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        derivative = session.scalar(select(DocumentDerivative))
        assert derivative.content.startswith(b"%PDF-")
        assert derivative.converter_version == "fake-converter-v1"


def test_conversion_failure_requires_review_and_keeps_the_original(context, monkeypatch):
    def unavailable(*args):
        raise ProviderError("CONVERSION_FAILED")

    monkeypatch.setattr(context.providers.converter, "convert", unavailable)
    uploaded = context.upload(DOC_CONTENT, DOC).json()["upload_id"]
    context.drain()
    status = context.client.get(
        f"{context.base}/cv-uploads/{uploaded}", headers=context.headers
    ).json()
    assert status["status"] == "needs_review" and status["application_id"] is None
    assert status["review_reason"] == "CONVERSION_FAILED"
    original = context.client.get(
        f"{context.base}/cv-uploads/{uploaded}/file", headers=context.headers
    )
    assert original.content == DOC_CONTENT


def test_gmail_verified_notifications_are_idempotent_and_wrong_issuers_fail(context, monkeypatch):
    from recruitment.services import mail_notifications

    connect(context)
    context.drain()
    mailbox = context.client.get(
        f"{context.base}/mailboxes",
        headers=context.headers,
    ).json()["items"][0]
    context.settings.mailbox_provider_mode = "real"
    context.settings.gmail_push_audience = (
        "https://recruitment.example.org/api/v1/mailbox-webhooks/gmail"
    )
    context.settings.gmail_push_service_account = "push@example.iam.gserviceaccount.com"
    claims = {
        "iss": "https://accounts.google.com",
        "email_verified": True,
        "email": context.settings.gmail_push_service_account,
    }
    monkeypatch.setattr(
        mail_notifications.id_token,
        "verify_oauth2_token",
        lambda *args, **kwargs: claims,
    )
    payload = {
        "message": {
            "messageId": "verified-provider-event",
            "data": base64.b64encode(
                json.dumps({"emailAddress": mailbox["email"]}).encode()
            ).decode(),
        }
    }
    for _ in range(2):
        assert (
            context.client.post(
                "/api/v1/mailbox-webhooks/gmail",
                json=payload,
                headers={"Authorization": "Bearer local-contract-proof"},
            ).status_code
            == 202
        )
    claims["iss"] = "https://untrusted.example.org"
    assert (
        context.client.post(
            "/api/v1/mailbox-webhooks/gmail",
            json=payload,
            headers={"Authorization": "Bearer local-contract-proof"},
        ).status_code
        == 401
    )
    db = context.client.app.state.container.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        assert session.scalar(select(func.count()).select_from(MailboxReceipt)) == 1
