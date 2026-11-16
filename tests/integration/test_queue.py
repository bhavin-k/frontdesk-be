from uuid import UUID

import psycopg
import pytest
from sqlalchemy import func, select

from recruitment.adapters.db.models import Job, Outbox
from recruitment.adapters.queue import LeaseLost
from recruitment.services.common import emit
from tests.support import Context


def test_expired_lease_is_reclaimed_and_stale_worker_is_fenced(
    context: Context, cv: bytes, admin_url: str
) -> None:
    context.upload(cv)
    queue = context.worker().queue
    assert queue.dispatch() == 1
    first = queue.claim()
    assert first is not None
    assert queue.claim() is None
    assert queue.renew(first)
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "UPDATE job_runs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (first.id,),
        )
    second = queue.claim()
    assert second is not None
    assert second.id == first.id and second.lease_token != first.lease_token
    assert second.attempts == 2
    assert not queue.renew(first)
    with pytest.raises(LeaseLost), queue.owned(first):
        pass
    context.worker().processor.process(second)
    queue.finish(second)


def test_outbox_dispatch_and_processing_are_idempotent(context: Context, cv: bytes) -> None:
    response = context.upload(cv)
    queue = context.worker().queue
    queue.dispatch()
    queue.dispatch()
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert (
            session.scalar(select(func.count()).select_from(Job).where(Job.kind == "cv.uploaded"))
            == 1
        )
    lease = queue.claim()
    assert lease is not None
    processor = context.worker().processor
    processor.process(lease)
    processor.process(lease)
    queue.finish(lease)
    assert context.result(response.json()["upload_id"])["score"] == 100


def test_outbox_rolls_back_with_domain_transaction(context: Context) -> None:
    db = context.client.app.state.container.db
    with (
        pytest.raises(RuntimeError),
        db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session,
    ):
        emit(session, UUID(context.tenant), "cv.uploaded", UUID(context.opening))
        session.flush()
        raise RuntimeError("Simulated transaction failure.")
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Outbox).where(Outbox.kind == "cv.uploaded")
            )
            == 0
        )
