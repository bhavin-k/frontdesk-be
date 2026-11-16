from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import func, select

from recruitment.adapters.db.models import (
    Application,
    Candidate,
    CriteriaVersion,
    MatchRun,
)
from recruitment.adapters.db.openings import OpeningIntakePeriod
from recruitment.adapters.db.phase2 import IdentityReview
from recruitment.core.time import utcnow
from recruitment.domain.openings import ExtractedRequirement, GeneratedRequirements
from recruitment.ports.mailbox import CursorExpired, MessagePage
from tests.integration.test_mail_intake import connect, fake, new_mail, sync
from tests.integration.test_retention import expire
from tests.support import identity, pdf_bytes


def create(context, ready=True, **fields):
    response = context.client.post(
        f"{context.base}/openings",
        headers=context.headers,
        json={"title": "Another Python role", "job_description": "Python development", **fields},
    )
    assert response.status_code == 201, response.text
    row = response.json()
    if ready:
        context.drain()
        row = read(context, row)
    return row


def read(context, row):
    response = context.client.get(f"{context.base}/openings/{row['id']}", headers=context.headers)
    assert response.status_code == 200
    return response.json()


def action(context, row, value):
    response = context.client.post(
        f"{context.base}/openings/{row['id']}/actions",
        headers=context.headers,
        json={"action": value, "expected_revision": row["revision"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_minimal_job_and_multiple_active_openings(context):
    row = create(context, ready=False)
    assert row["status"] == "active" and row["criteria_status"] == "queued"
    assert row["criteria"] == [] and row["documents"] == []
    context.drain()
    prepared = read(context, row)
    assert prepared["criteria_status"] == "ready"
    assert prepared["shortlist_threshold"] == 75
    assert all(c["weight"] == 1 and c["source_evidence"] for c in prepared["criteria"])
    assert prepared["criteria_metadata"]["model"] == "fake-job-criteria-v1"
    assert (
        len(
            context.client.get(
                f"{context.base}/openings?status=active", headers=context.headers
            ).json()["items"]
        )
        == 2
    )
    rejected = context.client.post(
        f"{context.base}/openings",
        headers=context.headers,
        json={
            "title": "Job",
            "job_description": "Python development",
            "criteria": [],
            "shortlist_threshold": 1,
        },
    )
    assert rejected.status_code == 422


def test_details_questions_versions_and_stale_updates(context):
    row = create(
        context,
        details={
            "department": "Engineering",
            "location": "Pune",
            "total_experience_years": 3,
            "relevant_experience_years": 2,
            "headcount": 2,
            "salary_min": 500000,
            "salary_max": 900000,
            "application_deadline": "2027-01-01T10:00:00+05:30",
        },
        questions=["Describe an API failure.", "Explain a database migration."],
    )
    assert row["details"]["application_deadline"].endswith("Z") or row["details"][
        "application_deadline"
    ].endswith("+00:00")
    payload = {
        "title": row["title"],
        "job_description": row["job_description"],
        "details": row["details"],
        "questions": list(reversed(row["questions"])),
        "expected_revision": row["revision"],
    }
    path = f"{context.base}/openings/{row['id']}"
    edited = context.client.patch(path, headers=context.headers, json=payload)
    assert edited.status_code == 200
    assert edited.json()["questions"] == payload["questions"]
    assert edited.json()["criteria_version_id"] == row["criteria_version_id"]
    assert context.client.patch(path, headers=context.headers, json=payload).status_code == 409
    updated = context.client.patch(
        path,
        headers=context.headers,
        json={
            **payload,
            "expected_revision": edited.json()["revision"],
            "job_description": "PostgreSQL development",
        },
    )
    assert updated.status_code == 200 and updated.json()["criteria_status"] == "queued"
    context.drain()
    assert read(context, row)["criteria_version_id"] != row["criteria_version_id"]
    with context.worker().queue.db.transaction(
        context.worker().queue.db.worker, UUID(context.tenant)
    ) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(CriteriaVersion)
                .where(CriteriaVersion.opening_id == UUID(row["id"]))
            )
            == 2
        )


def test_lifecycle_blocks_uploads_and_closed_reopening(context, cv):
    row = create(context)
    scoped = replace(context, opening=row["id"])
    paused = action(context, row, "pause")
    assert paused["status"] == "paused" and scoped.upload(cv).status_code == 409
    assert (
        context.client.get(
            f"{context.base}/openings?status=paused", headers=context.headers
        ).json()["items"][0]["id"]
        == row["id"]
    )
    active = action(context, paused, "continue")
    scoped.upload(cv)
    # Accepted work continues through a pause.
    paused = action(context, active, "pause")
    scoped.drain()
    closed = action(context, paused, "close")
    context.drain()
    assert read(context, closed)["status"] == "closed"
    assert scoped.upload(cv).status_code == 409
    assert (
        context.client.post(
            f"{context.base}/openings/{row['id']}/actions",
            headers=context.headers,
            json={"action": "continue", "expected_revision": closed["revision"]},
        ).status_code
        == 409
    )
    with context.worker().queue.db.transaction(
        context.worker().queue.db.worker, UUID(context.tenant)
    ) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(OpeningIntakePeriod)
                .where(OpeningIntakePeriod.opening_id == UUID(row["id"]))
            )
            == 2
        )
        assert session.scalar(select(func.count()).select_from(Application)) == 1


def test_concurrent_actions_use_one_revision(context):
    row = create(context)

    def apply(value):
        return context.client.post(
            f"{context.base}/openings/{row['id']}/actions",
            headers=context.headers,
            json={"action": value, "expected_revision": row["revision"]},
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(apply, ["pause", "close"])) == [200, 409]


@pytest.mark.parametrize("delayed", [False, True])
def test_mail_received_during_pause_is_not_imported_after_continue(context, delayed):
    mailbox = connect(context)
    context.drain()
    row = context.client.get(
        f"{context.base}/openings/{context.opening}", headers=context.headers
    ).json()
    row = action(context, row, "pause")
    new_mail(
        context, pdf_bytes("Paused candidate\npaused@example.org\nPython development"), utcnow()
    )
    if not delayed:
        sync(context, mailbox)
    row = action(context, row, "continue")
    sync(context, mailbox)
    assert (
        context.client.get(f"{context.base}/candidates", headers=context.headers).json()["items"]
        == []
    )
    new_mail(context, pdf_bytes("Active candidate\nactive@example.org\nPython development"))
    sync(context, mailbox)
    assert (
        len(
            context.client.get(f"{context.base}/candidates", headers=context.headers).json()[
                "items"
            ]
        )
        == 1
    )


def test_pause_boundary_survives_cursor_recovery_and_worker_restart(context, monkeypatch):
    mailbox = connect(context)
    context.drain()
    row = read(context, {"id": context.opening})
    row = action(context, row, "pause")
    paused = new_mail(
        context, pdf_bytes("Skipped candidate\nskipped@example.org\nPython development")
    )
    row = action(context, row, "continue")
    current = new_mail(
        context, pdf_bytes("Current candidate\ncurrent@example.org\nPython development")
    )
    provider = fake(context)
    calls = 0

    def recovering(credentials, folder, cursor, after):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CursorExpired
        return MessagePage([paused, current], {"offset": len(provider.messages)}, True)

    monkeypatch.setattr(provider, "page", recovering)
    # sync drains a new worker instance: eligibility comes from durable intake periods.
    sync(context, mailbox)
    assert calls >= 2
    assert provider.downloads == [(current.id, "cv")]
    candidates = context.client.get(f"{context.base}/candidates", headers=context.headers).json()
    assert len(candidates["items"]) == 1
    assert candidates["items"][0]["profile"]["email"] == "current@example.org"


def test_preparation_failure_then_retry_releases_waiting_cvs(context, cv, monkeypatch):
    row = create(context, ready=False)
    scoped = replace(context, opening=row["id"])
    upload = scoped.upload(cv).json()
    adapter = context.providers.job_criteria
    original = adapter.requirements
    monkeypatch.setattr(
        adapter,
        "requirements",
        lambda sources: GeneratedRequirements(
            criteria=[
                ExtractedRequirement(
                    text="Python development",
                    required=True,
                    source_id="description",
                    quote="Invented quote",
                )
            ]
        ),
    )
    context.drain()
    failed = read(context, row)
    assert failed["criteria_status"] == "failed"
    status = context.client.get(
        f"{context.base}/cv-uploads/{upload['upload_id']}", headers=context.headers
    ).json()
    assert status["status"] == "needs_review" and status["application_id"] is None
    monkeypatch.setattr(adapter, "requirements", original)
    retry = context.client.post(
        f"{context.base}/openings/{row['id']}/criteria/retry",
        headers=context.headers,
        json={"expected_revision": failed["revision"]},
    )
    assert retry.status_code == 202
    context.drain()
    assert scoped.result(upload["upload_id"])["score"] == 100


def test_close_during_matching_cannot_publish_shortlist(context, cv, monkeypatch):
    upload = context.upload(cv).json()["upload_id"]
    row = context.client.get(
        f"{context.base}/openings/{context.opening}", headers=context.headers
    ).json()
    original = context.providers.grader.grade

    def close_during_call(*args):
        action(context, row, "close")
        return original(*args)

    monkeypatch.setattr(context.providers.grader, "grade", close_during_call)
    context.drain()
    status = context.client.get(
        f"{context.base}/cv-uploads/{upload}", headers=context.headers
    ).json()
    assert status["status"] == "needs_review" and status["review_reason"] == "OPENING_CLOSED"
    with context.worker().queue.db.transaction(
        context.worker().queue.db.worker, UUID(context.tenant)
    ) as session:
        assert session.scalar(select(func.count()).select_from(MatchRun)) == 0
        assert session.scalar(select(Application.status)) != "rejected"


def test_cross_role_duplicates_and_updates_preserve_current_candidate(context, cv):
    uploaded = context.upload(cv).json()["upload_id"]
    context.drain()
    row = create(context)
    # Preparing a new opening re-evaluates undecided CVs before either duplicate arrives.
    original = context.result(uploaded)
    other = replace(context, opening=row["id"])
    assert other.upload(cv).status_code == 200
    other.upload(
        pdf_bytes("Changed Person\ncandidate@example.org\nPython development in a different CV")
    )
    context.drain()
    cases = context.client.get(f"{context.base}/identity-reviews", headers=context.headers).json()[
        "items"
    ]
    assert len([case for case in cases if case["kind"] == "cross_role_submission"]) == 2
    with context.worker().queue.db.transaction(
        context.worker().queue.db.worker, UUID(context.tenant)
    ) as session:
        assert session.scalar(select(func.count()).select_from(Application)) == 1
        assert session.scalar(select(Candidate.profile))["name"] == "Example Candidate"
    assert context.result(uploaded)["match_run_id"] == original["match_run_id"]
    resolved = context.client.post(
        f"{context.base}/identity-reviews/{cases[0]['id']}/resolve",
        headers={**context.headers, "Idempotency-Key": "keep-existing-opening"},
        json={"decision": "dismiss", "reason": "Keep the existing opening assignment."},
    )
    assert resolved.status_code == 202


def test_retention_removes_cross_role_review_without_deleting_openings(context, cv, admin_url):
    uploaded = context.upload(cv).json()["upload_id"]
    context.drain()
    original = context.result(uploaded)
    other = replace(context, opening=create(context)["id"])
    assert other.upload(cv).status_code == 200
    assert context.client.get(f"{context.base}/identity-reviews", headers=context.headers).json()[
        "items"
    ]
    expire(context, admin_url, original["application_id"])
    context.settings.auto_delete_enabled = True
    context.drain()
    with context.worker().queue.db.transaction(
        context.worker().queue.db.worker, UUID(context.tenant)
    ) as session:
        assert session.scalar(select(func.count()).select_from(IdentityReview)) == 0
        assert session.scalar(select(func.count()).select_from(Candidate)) == 0
    assert (
        len(context.client.get(f"{context.base}/openings", headers=context.headers).json()["items"])
        == 2
    )


def test_supporting_documents_are_scoped_deduplicated_and_bounded(context):
    content = pdf_bytes("Python development supporting requirements")
    response = context.client.post(
        f"{context.base}/openings",
        headers=context.headers,
        data={"payload": '{"title":"Python role","job_description":"Python development"}'},
        files=[("files", ("requirements.pdf", content, "application/pdf"))],
    )
    assert response.status_code == 201, response.text
    row = response.json()
    context.drain()
    row = read(context, row)
    document = row["documents"][0]
    path = f"{context.base}/openings/{row['id']}/documents"
    assert (
        context.client.get(f"{path}/{document['id']}/file", headers=context.headers).content
        == content
    )
    duplicate = context.client.post(
        path,
        headers=context.headers,
        data={"expected_revision": row["revision"]},
        files=[("files", ("same.pdf", content, "application/pdf"))],
    )
    assert duplicate.status_code == 201 and len(duplicate.json()["documents"]) == 1
    too_many = context.client.post(
        path,
        headers=context.headers,
        data={"expected_revision": row["revision"]},
        files=[("files", (f"{i}.pdf", content, "application/pdf")) for i in range(6)],
    )
    assert too_many.status_code == 422
    _, foreign_headers = identity(context.client)
    assert (
        context.client.get(f"{path}/{document['id']}/file", headers=foreign_headers).status_code
        == 404
    )
    removed = context.client.delete(
        f"{path}/{document['id']}?expected_revision={row['revision']}", headers=context.headers
    )
    assert removed.status_code == 200 and removed.json()["documents"] == []


@pytest.mark.parametrize(
    "details",
    [
        {"salary_min": 10, "salary_max": 1},
        {"headcount": 0},
        {"total_experience_years": 1, "relevant_experience_years": 3},
        {"application_deadline": "2027-01-01T12:00:00"},
    ],
)
def test_optional_details_are_validated(context, details):
    response = context.client.post(
        f"{context.base}/openings",
        headers=context.headers,
        json={"title": "Job", "job_description": "Python development", "details": details},
    )
    assert response.status_code == 422


def test_stale_requirements_cannot_publish_after_job_edit(context, monkeypatch):
    row = create(context, ready=False)
    adapter = context.providers.job_criteria
    original = adapter.requirements
    edited = False

    def change_source(sources):
        nonlocal edited
        result = original(sources)
        if not edited:
            edited = True
            changed = context.client.patch(
                f"{context.base}/openings/{row['id']}",
                headers=context.headers,
                json={
                    "title": row["title"],
                    "job_description": "PostgreSQL development",
                    "expected_revision": row["revision"],
                },
            )
            assert changed.status_code == 200
        return result

    monkeypatch.setattr(adapter, "requirements", change_source)
    context.drain()
    ready = read(context, row)
    assert ready["criteria_status"] == "ready" and ready["source_revision"] == 2
    assert all("PostgreSQL" in criterion["text"] for criterion in ready["criteria"])
    with context.worker().queue.db.transaction(
        context.worker().queue.db.worker, UUID(context.tenant)
    ) as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(CriteriaVersion)
                .where(CriteriaVersion.opening_id == UUID(row["id"]))
            )
            == 1
        )


def test_preparation_transient_failures_have_bounded_retries(context, cv, monkeypatch):
    from datetime import timedelta

    from recruitment.adapters.db.models import Job
    from recruitment.core.errors import ProviderError

    row = create(context, ready=False)
    scoped = replace(context, opening=row["id"])
    uploaded = scoped.upload(cv).json()["upload_id"]

    def unavailable(sources):
        raise ProviderError("AI_UNAVAILABLE", retryable=True)

    monkeypatch.setattr(context.providers.job_criteria, "requirements", unavailable)
    context.drain()
    assert read(context, row)["criteria_status"] == "queued"
    queue = context.worker().queue
    with queue.db.transaction(queue.db.worker, UUID(context.tenant)) as session:
        job = session.scalar(
            select(Job).where(Job.aggregate_id == UUID(row["id"]), Job.kind == "opening.prepare")
        )
        assert job.state == "retry_wait"
        job.attempts = context.settings.max_job_attempts - 1
        job.available_at = utcnow() - timedelta(seconds=1)
    context.drain()
    assert read(context, row)["criteria_status"] == "failed"
    status = context.client.get(
        f"{context.base}/cv-uploads/{uploaded}", headers=context.headers
    ).json()
    assert status["status"] == "needs_review" and status["application_id"] is None
