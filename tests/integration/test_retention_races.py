from uuid import UUID

import psycopg

from recruitment.services.common import emit
from tests.support import Context


def test_queued_expiry_respects_new_policy_before_recalculation(
    context: Context, cv: bytes, admin_url: str
) -> None:
    response = context.upload(cv)
    context.drain()
    result = context.result(response.json()["upload_id"])
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "UPDATE applications SET retention_expires_at = now() - interval '1 day', "
            "last_activity_at = now() - interval '40 days' WHERE id = %s",
            (result["application_id"],),
        )
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        emit(
            session,
            UUID(context.tenant),
            "retention.application_due",
            UUID(result["application_id"]),
        )
    worker = context.worker()
    worker.queue.dispatch()
    lease = worker.queue.claim()
    assert lease is not None and lease.kind == "retention.application_due"
    updated = context.client.patch(
        f"{context.base}/retention-settings",
        headers=context.headers,
        json={"other_candidate_retention_days": 60},
    )
    assert updated.status_code == 200
    context.settings.auto_delete_enabled = True
    worker.retention.delete_application(lease)
    worker.queue.finish(lease)
    context.drain()
    assert (
        context.client.get(
            f"{context.base}/applications/{result['application_id']}", headers=context.headers
        ).status_code
        == 200
    )
