from uuid import UUID

import pymupdf
import pytest
from sqlalchemy import select

from recruitment.adapters.db.models import (
    CVUpload,
    MatchRun,
    ResumeChunk,
    ResumeExtraction,
    ResumeFile,
)
from recruitment.adapters.db.openings import OpeningDocument
from recruitment.adapters.fakes import FakeOCR
from recruitment.core.errors import ProviderError
from tests.integration.test_mail_intake import connect, new_mail
from tests.support import identity, pdf_bytes, pdf_with_attachment


@pytest.mark.parametrize("email", [False, True])
def test_attached_pdf_keeps_original_and_matches_only_page_text(context, cv, monkeypatch, email):
    content = pdf_with_attachment(cv, b"HIDDEN_NON_CV_PAYLOAD hidden@example.invalid")
    scans = []
    scan = context.providers.scanner.scan

    def recording_scan(value):
        scans.append(value)
        scan(value)

    monkeypatch.setattr(context.providers.scanner, "scan", recording_scan)
    if email:
        connect(context)
        new_mail(context, content)
        context.drain()
        intake = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()
        identifier = intake["items"][0]["upload_id"]
        assert intake["items"][0]["status"] == "completed"
    else:
        response = context.upload(content)
        assert response.status_code == 202, response.text
        identifier = response.json()["upload_id"]
        context.drain()
    assert scans[0] == content and len(scans) == 2
    with pymupdf.open(stream=scans[1], filetype="pdf") as clean:
        assert clean.embfile_count() == 0
    result = context.result(identifier)
    assert result["score"] == 100 and result["criteria"][0]["evidence"]
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(ResumeFile.content)) == content
        extraction = session.scalar(select(ResumeExtraction))
        assert extraction.method == "pymupdf+pdf-pages-v1"
        assert extraction.profile["email"] == "candidate@example.org"
        assert all(
            "HIDDEN_NON_CV_PAYLOAD" not in chunk.text
            for chunk in session.scalars(select(ResumeChunk))
        )
    _, other_headers = identity(context.client)
    original = f"{context.base}/cv-uploads/{identifier}/file"
    assert context.client.get(original, headers=other_headers).status_code == 404
    assert context.client.get(original, headers=context.headers).content == content


def test_ocr_receives_only_the_attachment_free_pdf(context, monkeypatch):
    content = pdf_with_attachment(pdf_bytes(""))
    ocr = FakeOCR()
    calls = []

    def extract(value):
        with pymupdf.open(stream=value, filetype="pdf") as clean:
            assert clean.embfile_count() == 0
        calls.append(value)
        return ocr.extract(value)

    monkeypatch.setattr(context.providers.ocr, "extract", extract)
    uploaded = context.upload(content)
    assert uploaded.status_code == 202
    context.drain()
    assert len(calls) == 1
    assert context.result(uploaded.json()["upload_id"])["score"] == 100
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(ResumeExtraction.method)) == "fake-ocr+pdf-pages-v1"


@pytest.mark.parametrize("email", [False, True])
def test_original_is_scanned_before_discarding_attachments(context, cv, monkeypatch, email):
    content = pdf_with_attachment(cv)
    scanned = []

    def blocked(value):
        scanned.append(value)
        raise ProviderError("UNSAFE_FILE")

    monkeypatch.setattr(context.providers.scanner, "scan", blocked)
    if email:
        connect(context)
        new_mail(context, content)
        context.drain()
        intake = context.client.get(f"{context.base}/mail-intake", headers=context.headers).json()
        assert intake["items"][0]["failure_code"] == "UNSAFE_FILE"
    else:
        assert context.upload(content).status_code == 503
    assert scanned == [content]
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(CVUpload.id)) is None


def test_failed_sanitization_requires_review_without_creating_a_score(context, cv, monkeypatch):
    from recruitment.services import document_processing

    content = pdf_with_attachment(cv)

    def failed(value):
        raise ProviderError("PDF_SANITIZATION_FAILED")

    monkeypatch.setattr(document_processing, "processing_pdf", failed)
    response = context.upload(content)
    assert response.status_code == 202
    context.drain()
    status = context.client.get(
        f"{context.base}/cv-uploads/{response.json()['upload_id']}", headers=context.headers
    ).json()
    assert status["status"] == "needs_review"
    assert status["review_reason"] == "PDF_SANITIZATION_FAILED"
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(MatchRun.id)) is None


def test_supporting_job_pdf_uses_the_same_safe_processing_path(context):
    content = pdf_with_attachment(pdf_bytes("Python development experience is required."))
    response = context.client.post(
        f"{context.base}/openings",
        headers=context.headers,
        data={"payload": '{"title":"Python role","job_description":"Python development"}'},
        files=[("files", ("requirements.pdf", content, "application/pdf"))],
    )
    assert response.status_code == 201, response.text
    context.drain()
    row = context.client.get(
        f"{context.base}/openings/{response.json()['id']}", headers=context.headers
    ).json()
    assert row["criteria_status"] == "ready" and row["criteria"]
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        document = session.scalar(select(OpeningDocument))
        assert document.content == content
        assert document.extraction_method == "pymupdf+pdf-pages-v1"
