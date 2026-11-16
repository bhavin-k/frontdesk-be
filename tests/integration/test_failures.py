from uuid import UUID

import psycopg
import pytest
from sqlalchemy import select

from recruitment.adapters.db.models import Application, MatchRun
from recruitment.adapters.documents import DOCX
from recruitment.adapters.fakes import FakeEmbeddings, FakeOCR
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import EvidenceGrade
from tests.support import Context, docx_bytes, pdf_bytes


def upload_state(context: Context, upload_id: str):
    return context.client.get(
        f"{context.base}/cv-uploads/{upload_id}", headers=context.headers
    ).json()


@pytest.mark.parametrize("failure", ["extract", "profile", "embedding", "grader", "ocr"])
def test_processing_failure_requires_review_not_rejection(
    context: Context, cv: bytes, failure: str
) -> None:
    class FailingProvider:
        model = "fake-failure-v1"

        def extract(self, *args):
            raise ProviderError("TEST_EXTRACTION_FAILURE")

        def profile(self, *args):
            raise ProviderError("TEST_PROFILE_FAILURE")

        def embed(self, *args):
            raise ProviderError("TEST_EMBEDDING_FAILURE")

        def grade(self, *args):
            raise ProviderError("TEST_GRADER_FAILURE")

    attribute = {
        "extract": "extractor",
        "profile": "profiles",
        "embedding": "embeddings",
        "grader": "grader",
        "ocr": "ocr",
    }[failure]
    provider = FailingProvider()
    if failure == "embedding":
        provider.model = context.providers.embeddings.model
    setattr(context.providers, attribute, provider)
    if failure == "ocr":
        cv = pdf_bytes("")
    response = context.upload(cv)
    assert response.status_code == 202
    context.drain()
    state = upload_state(context, response.json()["upload_id"])
    assert state["status"] == "needs_review"
    assert state["review_reason"].startswith("TEST_")
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert not session.scalars(
            select(Application).where(Application.status == "rejected")
        ).all()
        assert not session.scalars(select(MatchRun)).all()
    queue = context.client.get(f"{context.base}/review-queue", headers=context.headers).json()
    assert queue["uploads"]


def test_retry_exhaustion_is_durable_and_goes_to_review(
    context: Context, cv: bytes, admin_url: str
) -> None:
    class UnavailableEmbeddings(FakeEmbeddings):
        calls = 0

        def embed(self, texts):
            self.calls += 1
            raise ProviderError("EMBEDDING_UNAVAILABLE", retryable=True)

    provider = UnavailableEmbeddings()
    context.providers.embeddings = provider
    response = context.upload(cv)
    worker = context.worker()
    assert worker.run_once()
    assert upload_state(context, response.json()["upload_id"])["status"] == "retry_wait"
    for _ in range(2):
        with psycopg.connect(admin_url) as connection:
            connection.execute(
                "UPDATE job_runs SET available_at = now() - interval '1 minute' "
                "WHERE tenant_id = %s AND state = 'retry_wait'",
                (context.tenant,),
            )
        assert worker.run_once()
    assert provider.calls == 3
    assert upload_state(context, response.json()["upload_id"])["status"] == "needs_review"
    with psycopg.connect(admin_url) as connection:
        row = connection.execute(
            "SELECT state, attempts, failure_code FROM job_runs WHERE kind = 'cv.uploaded'"
        ).fetchone()
        assert row == ("failed", 3, "EMBEDDING_UNAVAILABLE")
        assert connection.execute("SELECT count(*) FROM failed_jobs").fetchone()[0] == 1


@pytest.mark.parametrize("updated", [False, True])
def test_successful_retry_clears_errors_and_preserves_required_review(
    context: Context, cv: bytes, admin_url: str, updated: bool
) -> None:
    if updated:
        context.upload(cv)
        context.drain()
        cv = pdf_bytes(
            "Example Candidate\ncandidate@example.org\nPython development in a newer CV."
        )

    class RecoveringEmbeddings(FakeEmbeddings):
        calls = 0

        def embed(self, texts):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("EMBEDDING_UNAVAILABLE", retryable=True)
            return super().embed(texts)

    context.providers.embeddings = RecoveringEmbeddings()
    response = context.upload(cv)
    assert response.status_code == 202
    identifier = response.json()["upload_id"]
    assert context.worker().run_once()
    waiting = upload_state(context, identifier)
    assert waiting["status"] == "retry_wait"
    assert waiting["review_reason"] == ("RESUME_UPDATED" if updated else "EMBEDDING_UNAVAILABLE")
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "UPDATE job_runs SET available_at=now()-interval '1 minute' "
            "WHERE tenant_id=%s AND aggregate_id=%s AND state='retry_wait'",
            (context.tenant, identifier),
        )
    context.drain()
    result = context.result(identifier)
    assert result["recommendation"] == ("needs_review" if updated else "recommended_shortlist")
    assert upload_state(context, identifier)["review_reason"] == (
        "RESUME_UPDATED" if updated else None
    )
    with psycopg.connect(admin_url) as connection:
        assert connection.execute(
            "SELECT state,attempts,failure_code FROM job_runs "
            "WHERE tenant_id=%s AND aggregate_id=%s AND kind='cv.uploaded'",
            (context.tenant, identifier),
        ).fetchone() == ("succeeded", 2, None)


def test_invalid_embedding_dimensions_cannot_be_stored(context: Context, cv: bytes) -> None:
    class WrongDimensions(FakeEmbeddings):
        def embed(self, texts):
            return [[0.2] * 10 for _ in texts]

    context.providers.embeddings = WrongDimensions()
    response = context.upload(cv)
    context.drain()
    state = upload_state(context, response.json()["upload_id"])
    assert state["status"] == "needs_review"
    assert state["review_reason"] == "INVALID_EMBEDDING"


def test_non_english_cv_routes_to_review(context: Context) -> None:
    response = context.upload(docx_bytes("उम्मीदवार\nपायथन डेवलपर अनुभव और शिक्षा विवरण।"), DOCX)
    context.drain()
    state = upload_state(context, response.json()["upload_id"])
    assert state["status"] == "needs_review"
    assert state["review_reason"] == "unsupported_language"


def test_ocr_runs_for_scanned_pdf_only(context: Context, cv: bytes) -> None:
    import pymupdf

    ocr = FakeOCR("OCR Candidate\nocr@example.org\nPython development experience for three years.")
    context.providers.ocr = ocr
    first = context.upload(cv)
    context.drain()
    assert ocr.calls == 0
    assert context.result(first.json()["upload_id"])["score"] == 100
    document = pymupdf.open()
    document.new_page()
    scanned = document.tobytes()
    document.close()
    response = context.upload(scanned)
    context.drain()
    assert ocr.calls == 1
    assert context.result(response.json()["upload_id"])["score"] == 100


def test_docx_uses_local_extraction(context: Context) -> None:
    response = context.upload(
        docx_bytes(
            "DOCX Candidate\ndocx@example.org\nPython development experience for three years."
        ),
        DOCX,
    )
    context.drain()
    assert context.result(response.json()["upload_id"])["score"] == 100


def test_fabricated_citation_is_never_published(context: Context, cv: bytes) -> None:
    class InventedGrader:
        model = "fake-invented-grader"

        def grade(self, criterion, chunks):
            return EvidenceGrade(
                status="met",
                confidence=0.99,
                evidence_chunk_ids=[str(UUID(int=0))],
                quotes=["This text was never in the CV."],
                reason="Invented model claim.",
            )

    context.providers.grader = InventedGrader()
    response = context.upload(cv)
    context.drain()
    state = upload_state(context, response.json()["upload_id"])
    assert state["status"] == "needs_review"
    assert state["review_reason"] == "INVALID_EVIDENCE"


def test_unknown_requirements_remain_review_recommendations(context: Context) -> None:
    response = context.upload(
        pdf_bytes(
            "Unknown Candidate\nunknown@example.org\nExperience in unrelated warehouse operations."
        )
    )
    context.drain()
    result = context.result(response.json()["upload_id"])
    assert result["recommendation"] == "needs_review"
    assert result["score"] == 0
    assert result["score_provisional"] is True
