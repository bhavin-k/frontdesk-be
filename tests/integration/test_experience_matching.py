from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from openai import OpenAI
from sqlalchemy import select

from recruitment.adapters.db.models import Application, CVUpload, Job, Outbox, ReviewDecision
from recruitment.adapters.experience import EXPERIENCE_PROMPT_VERSION, LLMExperience
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from tests.support import identity, pdf_bytes


@pytest.mark.parametrize("strategy", ["rag", "full_text"])
def test_experience_request_over_budget_reviews_original_without_provider_call(
    context, cv, strategy
):
    calls = []

    def unexpected(request):
        calls.append(request)
        raise AssertionError("An oversized experience request must stay local.")

    context.settings.scoring_strategy = strategy
    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(unexpected)),
    ) as client:
        context.providers.experience = LLMExperience(
            client,
            "test-model",
            EXPERIENCE_PROMPT_VERSION,
            "chat_completions",
            "json_schema",
            request_tokens=1024,
            output_tokens=512,
        )
        upload = context.upload(cv).json()["upload_id"]
        context.drain()
    status = context.client.get(
        f"{context.base}/cv-uploads/{upload}", headers=context.headers
    ).json()
    assert status["status"] == "needs_review"
    assert status["review_reason"] == "EXPERIENCE_REQUEST_BUDGET_EXCEEDED"
    original = context.client.get(
        f"{context.base}/cv-uploads/{upload}/file", headers=context.headers
    )
    assert original.status_code == 200 and original.content == cv
    assert not calls


def role(context, title, description, **details):
    response = context.client.post(
        f"{context.base}/openings",
        headers=context.headers,
        json={"title": title, "job_description": description, "details": details},
    )
    assert response.status_code == 201, response.text
    context.drain()
    return response.json()["id"]


def change_primary(context, description="2 years relevant experience; Python development"):
    response = context.client.patch(
        f"{context.base}/openings/{context.opening}",
        headers=context.headers,
        json={"title": "Senior Python", "job_description": description, "expected_revision": 1},
    )
    assert response.status_code == 200
    context.drain()


def received_cv():
    year = utcnow().year
    return pdf_bytes(
        "Example Candidate\ncandidate@example.org\n"
        f"Software Engineer Intern {year - 2}-01 to {year - 2}-07 Python\n"
        f"Software Engineer {year - 1}-07 to Present Python development"
    )


def current(context, upload):
    status = context.client.get(
        f"{context.base}/cv-uploads/{upload}", headers=context.headers
    ).json()
    app = context.client.get(
        f"{context.base}/applications/{status['application_id']}", headers=context.headers
    ).json()
    result = context.client.get(
        f"{context.base}/applications/{app['id']}/match-results", headers=context.headers
    ).json()
    return status, app, result


def decide(context, app, evaluation, decision, assessment=None, confirmed=False, key=None):
    return context.client.post(
        f"{context.base}/applications/{app['id']}/review-decisions",
        headers={**context.headers, "Idempotency-Key": key or str(uuid4())},
        json={
            "evaluation_id": evaluation["id"],
            "expected_version": app["version"],
            "role_assessment_id": assessment,
            "decision": decision,
            "confirm_rejection": confirmed,
        },
    )


def test_failed_experience_skips_embeddings_and_waits_for_confirmed_rejection(context, monkeypatch):
    change_primary(context)

    def unexpected(*args):
        raise AssertionError("Failed experience must not call embeddings or detailed grading")

    monkeypatch.setattr(context.providers.embeddings, "embed", unexpected)
    monkeypatch.setattr(context.providers.grader, "grade", unexpected)
    uploaded = context.upload(received_cv()).json()["upload_id"]
    context.drain()
    status, app, data = current(context, uploaded)
    evaluation = data["evaluation"]
    assert status["status"] == "completed" and app["status"] == "needs_review"
    assert evaluation["recommendation"] == "recommended_rejection"
    assert evaluation["roles"][0]["eligibility"]["professional_months"] < 24
    assert evaluation["roles"][0]["score"] is None and data["result"] is None
    assert decide(context, app, evaluation, "reject").status_code == 422
    saved = decide(context, app, evaluation, "reject", confirmed=True)
    assert saved.status_code == 201, saved.text
    assert saved.json()["status"] == "rejected"
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        record = session.scalar(select(ReviewDecision))
        candidate_app = session.get(Application, UUID(app["id"]))
        assert record.reason is None and candidate_app.rejected_at is not None
        assert candidate_app.retention_expires_at - candidate_app.rejected_at == timedelta(days=7)


def test_alternative_role_selection_is_one_application_and_requires_save(context):
    change_primary(context)
    alternative = role(context, "Junior Python", "Python development")
    uploaded = context.upload(received_cv()).json()["upload_id"]
    context.drain()
    _, app, data = current(context, uploaded)
    evaluation = data["evaluation"]
    assert evaluation["recommendation"] == "recommended_shortlist"
    chosen = next(item for item in evaluation["roles"] if item["opening_id"] == alternative)
    assert chosen["score"] == 100 and app["opening_id"] == context.opening
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(Outbox.id).where(Outbox.kind == "shortlist.ready")) is None
        assert len(list(session.scalars(select(Application)))) == 1
    key = str(uuid4())
    saved = decide(context, app, evaluation, "shortlist", chosen["id"], key=key)
    assert saved.status_code == 201, saved.text
    assert decide(context, app, evaluation, "shortlist", chosen["id"], key=key).status_code == 200
    _, selected, _ = current(context, uploaded)
    assert selected["opening_id"] == alternative and selected["status"] == "shortlisted"
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.get(CVUpload, UUID(uploaded)).opening_id == UUID(context.opening)
        assert (
            len(list(session.scalars(select(Outbox.id).where(Outbox.kind == "shortlist.ready"))))
            == 1
        )


def test_equal_best_roles_require_review_and_stale_saves_are_rejected(context, cv):
    other = role(context, "Other Python", "Python development")
    uploaded = context.upload(cv).json()["upload_id"]
    context.drain()
    _, app, data = current(context, uploaded)
    evaluation = data["evaluation"]
    assert evaluation["recommendation"] == "needs_review"
    chosen = next(item for item in evaluation["roles"] if item["opening_id"] == other)
    bad_version = {**app, "version": app["version"] - 1}
    assert decide(context, bad_version, evaluation, "shortlist", chosen["id"]).status_code == 409
    assert decide(context, app, evaluation, "keep_review").status_code == 422
    assert decide(context, app, evaluation, "shortlist", chosen["id"]).status_code == 201


def test_grading_retry_resumes_completed_criteria(context, cv):
    change_primary(context, "Python development; PostgreSQL experience")
    original = context.providers.grader.grade
    counts = {}

    def flaky(criterion, chunks):
        counts[criterion] = counts.get(criterion, 0) + 1
        if "PostgreSQL" in criterion and counts[criterion] == 1:
            raise ProviderError("AI_UNAVAILABLE", retryable=True)
        return original(criterion, chunks)

    context.providers.grader.grade = flaky
    uploaded = context.upload(cv).json()["upload_id"]
    context.drain()
    db = context.client.app.state.container.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        job = session.scalar(
            select(Job).where(Job.kind == "cv.uploaded", Job.aggregate_id == UUID(uploaded))
        )
        assert job.state == "retry_wait"
        job.available_at = utcnow()
    context.drain()
    assert counts["Python development"] == 1 and counts["PostgreSQL experience"] == 2
    assert current(context, uploaded)[2]["evaluation"]["state"] == "completed"


def test_experience_provider_failure_is_review_with_no_score(context, cv):
    def unavailable(*args):
        raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")

    context.providers.experience.extract = unavailable
    uploaded = context.upload(cv).json()["upload_id"]
    context.drain()
    status, _, data = current(context, uploaded)
    assert status["status"] == "needs_review"
    assert data["evaluation"]["recommendation"] == "needs_review"
    assert data["evaluation"]["confidence"] is None
    assert all(item["score"] is None for item in data["evaluation"]["roles"])


def test_current_evaluation_and_role_reads_are_tenant_and_application_scoped(context, cv):
    first = context.upload(cv).json()["upload_id"]
    context.drain()
    _, app, data = current(context, first)
    assessment = data["evaluation"]["roles"][0]
    _, foreign_headers = identity(context.client)
    path = f"{context.base}/applications/{app['id']}/role-assessments/{assessment['id']}"
    assert context.client.get(path, headers=foreign_headers).status_code == 404
    second = context.upload(
        pdf_bytes("Other Candidate\nother@example.org\nPython development")
    ).json()["upload_id"]
    context.drain()
    _, other, _ = current(context, second)
    wrong_app = f"{context.base}/applications/{other['id']}/role-assessments/{assessment['id']}"
    assert context.client.get(wrong_app, headers=context.headers).status_code == 404
    assert decide(context, other, data["evaluation"], "reject", confirmed=True).status_code == 404


def test_two_simultaneous_saves_cannot_apply_the_same_application_version(context, cv):
    uploaded = context.upload(cv).json()["upload_id"]
    context.drain()
    _, app, data = current(context, uploaded)
    evaluation = data["evaluation"]
    assessment = evaluation["roles"][0]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: decide(context, app, evaluation, "shortlist", assessment).status_code,
                range(2),
            )
        )
    assert sorted(results) == [201, 409]


def test_recruiter_can_select_an_experience_rejection_without_inventing_a_reason(context):
    change_primary(context)
    uploaded = context.upload(received_cv()).json()["upload_id"]
    context.drain()
    _, app, data = current(context, uploaded)
    evaluation = data["evaluation"]
    assert evaluation["recommendation"] == "recommended_rejection"
    selected = decide(context, app, evaluation, "shortlist", evaluation["roles"][0]["id"])
    assert selected.status_code == 201, selected.text
    _, saved, unchanged = current(context, uploaded)
    assert (
        saved["status"] == "shortlisted"
        and unchanged["evaluation"]["recommendation"] == "recommended_rejection"
    )
    # A new opening must never re-evaluate or replace the saved final choice.
    previous_evaluation = saved["latest_evaluation_id"]
    role(context, "Another Python", "Python development")
    assert current(context, uploaded)[1]["latest_evaluation_id"] == previous_evaluation


def test_database_blocks_worker_final_selection_even_for_a_perfect_match(context, cv):
    from sqlalchemy.exc import DBAPIError

    uploaded = context.upload(cv).json()["upload_id"]
    context.drain()
    _, app, _ = current(context, uploaded)
    db = context.client.app.state.container.db
    with (
        pytest.raises(DBAPIError, match="Explicit recruiter selection required"),
        db.transaction(db.worker, UUID(context.tenant)) as session,
    ):
        session.get(Application, UUID(app["id"])).status = "shortlisted"
