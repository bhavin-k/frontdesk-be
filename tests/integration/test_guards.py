from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from recruitment.adapters.db.models import Application, JobCriterion
from recruitment.core.errors import ProviderError
from tests.support import Context, identity, pdf_bytes


def test_every_recruiter_can_upload_and_record_decisions(context: Context, cv: bytes) -> None:
    member_id, headers = identity(context.client)
    assert (
        context.client.post(
            f"{context.base}/members", headers=context.headers, json={"user_id": member_id}
        ).status_code
        == 201
    )
    context.headers = headers
    response = context.upload(cv)
    assert response.status_code == 202
    context.drain()
    result = context.result(response.json()["upload_id"])
    confirmed = context.client.post(
        f"{context.base}/applications/{result['application_id']}/review-decisions",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "match_run_id": result["match_run_id"],
            "decision": "reject",
            "confirm_rejection": True,
            "reason": "The recruiter reviewed the evidence.",
        },
    )
    assert confirmed.status_code == 201
    assert confirmed.json()["status"] == "rejected"
    db = context.client.app.state.container.db
    with (
        pytest.raises(DBAPIError),
        db.transaction(db.worker, UUID(context.tenant)) as session,
    ):
        session.execute(
            update(Application)
            .where(Application.id == UUID(result["application_id"]))
            .values(status="needs_review", rejected_at=None)
        )


def test_vector_model_mismatch_cannot_be_scored(context: Context, cv: bytes) -> None:
    context.upload(cv)
    worker = context.worker()
    worker.queue.dispatch()
    lease = worker.queue.claim()
    assert lease is not None
    with worker.queue.owned(lease) as session:
        criterion = session.scalar(select(JobCriterion))
        assert criterion is not None and criterion.embedding is not None
        criterion.embedding_model = "another-vector-model"
    with pytest.raises(ProviderError, match="EMBEDDING_MODEL_MISMATCH"):
        worker.processor.process(lease)


def test_worker_cannot_change_published_criteria(context: Context) -> None:
    db = context.client.app.state.container.db
    with (
        pytest.raises(DBAPIError),
        db.transaction(db.worker, UUID(context.tenant)) as session,
    ):
        session.execute(update(JobCriterion).values(text="Changed by worker"))


def test_database_rejects_misattributed_latest_match(context: Context, cv: bytes) -> None:
    first = context.upload(cv)
    context.drain()
    first_result = context.result(first.json()["upload_id"])
    second = context.upload(
        pdf_bytes(
            "Other Candidate\nother@example.org\nPython development for another business role."
        )
    )
    context.drain()
    second_result = context.result(second.json()["upload_id"])
    db = context.client.app.state.container.db
    with (
        pytest.raises(DBAPIError),
        db.transaction(db.worker, UUID(context.tenant)) as session,
    ):
        session.execute(
            update(Application)
            .where(Application.id == UUID(second_result["application_id"]))
            .values(latest_match_id=UUID(first_result["match_run_id"]))
        )
