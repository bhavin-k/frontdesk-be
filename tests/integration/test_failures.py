from uuid import UUID

import psycopg
import pytest
from sqlalchemy import select

from recruitment.adapters.db.matching import RoleAssessment
from recruitment.adapters.db.models import Application
from recruitment.adapters.fakes import FakeMatcher
from recruitment.core.errors import ProviderError
from tests.support import Context, pdf_bytes


def upload_state(context: Context, upload_id: str):
    return context.client.get(
        f"{context.base}/cv-uploads/{upload_id}", headers=context.headers
    ).json()


@pytest.mark.parametrize("failure", ["profile", "ocr", "matcher"])
def test_processing_failure_requires_review_not_rejection(
    context: Context, cv: bytes, failure: str
) -> None:
    class FailingProvider:
        model = "fake-failure-v1"

        def extract(self, *args):
            raise ProviderError("TEST_EXTRACTION_FAILURE")

        def profile(self, *args):
            raise ProviderError("TEST_PROFILE_FAILURE")

        def match(self, *args):
            raise ProviderError("TEST_MATCH_FAILURE")

    attribute = {"profile": "profiles", "matcher": "matcher", "ocr": "ocr"}[failure]
    setattr(context.providers, attribute, FailingProvider())
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
        assert not session.scalars(
            select(RoleAssessment).where(RoleAssessment.match_result.is_not(None))
        ).all()
    queue = context.client.get(f"{context.base}/review-queue", headers=context.headers).json()
    assert queue["uploads"]


def test_retry_exhaustion_is_durable_and_goes_to_review(
    context: Context, cv: bytes, admin_url: str
) -> None:
    class UnavailableMatcher(FakeMatcher):
        calls = 0

        def match(self, job, profile):
            self.calls += 1
            raise ProviderError("AI_UNAVAILABLE", retryable=True)

    provider = UnavailableMatcher()
    context.providers.matcher = provider
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
        assert row == ("failed", 3, "AI_UNAVAILABLE")
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

    class RecoveringMatcher(FakeMatcher):
        calls = 0

        def match(self, job, profile):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("AI_UNAVAILABLE", retryable=True)
            return super().match(job, profile)

    context.providers.matcher = RecoveringMatcher()
    response = context.upload(cv)
    assert response.status_code == 202
    identifier = response.json()["upload_id"]
    assert context.worker().run_once()
    waiting = upload_state(context, identifier)
    assert waiting["status"] == "retry_wait"
    assert waiting["review_reason"] == ("RESUME_UPDATED" if updated else "AI_UNAVAILABLE")
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
