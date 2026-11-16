from uuid import UUID

import psycopg
from sqlalchemy import func, select

from recruitment.adapters.db.models import (
    CriteriaVersion,
    Job,
    JobCriterion,
    MatchRun,
    ResumeChunk,
    ResumeExtraction,
)
from recruitment.adapters.documents import DOCX, PDF, LocalExtractor
from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.tokenization import token_count
from tests.integration.test_experience_matching import current, decide, role
from tests.integration.test_opening_management import expire
from tests.support import docx_bytes, identity


def test_strategy_survives_toggle_and_both_result_formats_support_decisions(
    context, cv, monkeypatch
):
    legacy = context.upload(cv).json()["upload_id"]
    context.settings.scoring_strategy = "full_text"
    context.drain()
    legacy_report = context.result(legacy)
    assert legacy_report["scoring_strategy"] == "rag"
    assert legacy_report["criteria"][0]["evidence"][0]["chunk_id"]
    assert legacy_report["embedding_dimensions"] == 1536

    assert context.providers.full_text_grader is not None
    original = context.providers.full_text_grader.grade
    grade_calls = []

    def grade(rubric, document):
        grade_calls.append(rubric)
        return original(rubric, document)

    def forbidden(*_args):
        raise AssertionError("A full-text CV job must not request embeddings or legacy grading.")

    monkeypatch.setattr(context.providers.full_text_grader, "grade", grade)
    monkeypatch.setattr(context.providers.embeddings, "embed", forbidden)
    monkeypatch.setattr(context.providers.grader, "grade", forbidden)
    cv = docx_bytes("Example Two\nexample.two@example.org\nDelivered Python development.")
    upload = context.upload(cv, DOCX).json()["upload_id"]
    context.settings.scoring_strategy = "rag"
    context.drain()
    assert len(grade_calls) == 1
    status, application, record = current(context, upload)
    assert status["status"] == "completed"
    match = record["result"]
    assert match["scoring_strategy"] == "full_text" and match["score"] == 100
    assert match["embedding_model"] is None and match["embedding_dimensions"] is None
    evidence = match["criteria"][0]["evidence"][0]
    assert evidence["source_kind"] == "document" and evidence["page"] is None
    db = context.worker().queue.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        extraction = session.scalar(
            select(ResumeExtraction).where(ResumeExtraction.upload_id == UUID(upload))
        )
        source = extraction.pages[0]["text"]
        assert source[evidence["char_start"] : evidence["char_end"]] == evidence["quote"]
        assert session.scalar(select(func.count()).select_from(ResumeChunk)) == 1
        job = session.scalar(
            select(Job).where(Job.aggregate_id == UUID(upload), Job.kind == "cv.uploaded")
        )
        assert job.scoring_strategy == "full_text"
    evaluation = record["evaluation"]
    assert (
        decide(
            context, application, evaluation, "shortlist", evaluation["roles"][0]["id"]
        ).status_code
        == 201
    )
    _, other = identity(context.client)
    assert context.client.get(
        f"{context.base}/applications/{application['id']}/match-results",
        headers=other,
    ).status_code in {403, 404}
    _, rag_app, rag_record = current(context, legacy)
    assert (
        decide(context, rag_app, rag_record["evaluation"], "reject", confirmed=True).status_code
        == 201
    )


def test_full_text_jd_edits_publish_new_rubric_and_retention_removes_results(
    context, cv, admin_url, monkeypatch
):
    context.settings.scoring_strategy = "full_text"
    upload = context.upload(cv).json()["upload_id"]
    context.drain()
    before = context.result(upload)
    seen = []
    adapter = context.providers.full_text_grader
    assert adapter is not None
    original = adapter.grade

    def grade(rubric, document):
        seen.append(rubric)
        return original(rubric, document)

    monkeypatch.setattr(adapter, "grade", grade)
    response = context.client.patch(
        f"{context.base}/openings/{context.opening}",
        headers=context.headers,
        json={
            "title": "Backend developer",
            "job_description": "Python development\nPostgreSQL migrations",
            "expected_revision": 1,
        },
    )
    assert response.status_code == 200
    context.drain()
    after = context.result(upload)
    assert len(seen) == 1 and seen[0].source_revision == 2 and len(seen[0].criteria) == 2
    assert after["criteria_version_id"] != before["criteria_version_id"]
    assert after["criteria"][0]["evidence"][0]["page"] == 1
    expire(context, admin_url, after["application_id"])
    context.settings.auto_delete_enabled = True
    context.drain()
    db = context.worker().queue.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        assert session.scalar(select(func.count()).select_from(MatchRun)) == 0


def test_oversized_cv_keeps_original_and_skips_ai(context, cv, monkeypatch):
    context.settings.scoring_strategy = "full_text"
    extracted = LocalExtractor().extract(cv, PDF)
    context.settings.cv_token_ceiling = token_count("\n".join(p.text for p in extracted.pages)) - 1

    def forbidden(*_args):
        raise AssertionError("Oversized CV must go to review before profiling.")

    monkeypatch.setattr(context.providers.profiles, "profile", forbidden)
    upload = context.upload(cv).json()["upload_id"]
    context.drain()
    response = context.client.get(f"{context.base}/cv-uploads/{upload}", headers=context.headers)
    assert response.json()["review_reason"] == "CV_TOKEN_CEILING_EXCEEDED"
    assert response.json()["status"] == "needs_review"
    original = context.client.get(
        f"{context.base}/cv-uploads/{upload}/file", headers=context.headers
    )
    assert original.status_code == 200 and original.content == cv


def test_full_text_retry_resumes_completed_roles_and_persists_retry_after(
    context, cv, admin_url, monkeypatch
):
    context.settings.scoring_strategy = "full_text"
    second = role(context, "Other Python opening", "Python development")
    adapter = context.providers.full_text_grader
    assert adapter is not None
    original = adapter.grade
    calls = []
    first_id, second_id = sorted((context.opening, second))

    def grade(rubric, document):
        calls.append(str(rubric.opening_id))
        if calls == [first_id, second_id]:
            error = ProviderError("AI_UNAVAILABLE", retryable=True)
            error.retry_after_seconds = 120
            raise error
        return original(rubric, document)

    monkeypatch.setattr(adapter, "grade", grade)
    uploaded = context.upload(cv).json()["upload_id"]
    context.drain()
    with psycopg.connect(admin_url) as connection:
        row = connection.execute(
            "SELECT state,available_at FROM job_runs WHERE kind='cv.uploaded' AND aggregate_id=%s",
            (uploaded,),
        ).fetchone()
        assert row[0] == "retry_wait" and (row[1] - utcnow()).total_seconds() > 100
        connection.execute(
            "UPDATE job_runs SET available_at=now()-interval '1 minute' "
            "WHERE kind='cv.uploaded' AND aggregate_id=%s",
            (uploaded,),
        )
    context.drain()
    assert calls == [first_id, second_id, second_id]
    assert context.result(uploaded)["score"] == 100


def test_full_text_opening_stores_rubric_for_lazy_rag_rollback(context, cv, monkeypatch):
    context.settings.scoring_strategy = "full_text"
    adapter = context.providers.embeddings

    def forbidden(*_args):
        raise AssertionError("Preparing a full-text rubric must not embed it.")

    with monkeypatch.context() as patch:
        patch.setattr(adapter, "embed", forbidden)
        opening_id = UUID(role(context, "New opening", "Python development"))
    db = context.worker().queue.db
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        criterion = session.scalar(
            select(JobCriterion)
            .join(
                CriteriaVersion,
                CriteriaVersion.id == JobCriterion.version_id,
            )
            .where(CriteriaVersion.opening_id == opening_id)
        )
        assert criterion is not None and criterion.embedding is None
    context.settings.scoring_strategy = "rag"
    upload = context.upload(cv).json()["upload_id"]
    context.drain()
    assert context.result(upload)["scoring_strategy"] == "rag"
    with db.transaction(db.worker, UUID(context.tenant)) as session:
        criterion = session.scalar(
            select(JobCriterion)
            .join(
                CriteriaVersion,
                CriteriaVersion.id == JobCriterion.version_id,
            )
            .where(CriteriaVersion.opening_id == opening_id)
        )
        assert criterion is not None and criterion.embedding is not None
