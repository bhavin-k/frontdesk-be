from datetime import timedelta
from uuid import uuid4

import psycopg

from tests.support import Context, pdf_bytes


def expire(context: Context, admin_url: str, application_id: str) -> None:
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "UPDATE applications SET retention_expires_at = now() - interval '1 day', "
            "last_activity_at = now() - interval '40 days', "
            "rejected_at = CASE WHEN status = 'rejected' THEN now() - interval '8 days' "
            "ELSE NULL END "
            "WHERE id = %s AND tenant_id = %s",
            (application_id, context.tenant),
        )


def test_confirmed_rejection_deletes_personal_content_and_keeps_only_tombstone(
    context: Context, cv: bytes, admin_url: str
) -> None:
    response = context.upload(cv)
    context.drain()
    result = context.result(response.json()["upload_id"])
    decision = context.client.post(
        f"{context.base}/applications/{result['application_id']}/review-decisions",
        headers={**context.headers, "Idempotency-Key": str(uuid4())},
        json={
            "match_run_id": result["match_run_id"],
            "decision": "reject",
            "confirm_rejection": True,
            "reason": "Recruiter confirmed the rejection.",
        },
    )
    assert decision.status_code == 201
    expire(context, admin_url, result["application_id"])
    context.settings.auto_delete_enabled = True
    context.drain()
    with psycopg.connect(admin_url) as connection:
        for table in [
            "candidates",
            "candidate_contacts",
            "applications",
            "resume_files",
            "resumes",
            "cv_uploads",
            "resume_extractions",
            "resume_chunks",
            "match_runs",
            "criterion_results",
            "review_decisions",
            "matching_evaluations",
            "role_assessments",
            "job_runs",
            "failed_jobs",
            "outbox_messages",
        ]:
            if table in {"job_runs", "failed_jobs", "outbox_messages"}:
                # Job preparation belongs to the retained opening, not the deleted candidate.
                count = connection.execute(
                    f"SELECT count(*) FROM {table} WHERE tenant_id = %s AND aggregate_id <> %s",
                    (context.tenant, context.opening),
                ).fetchone()[0]
            else:
                count = connection.execute(
                    f"SELECT count(*) FROM {table} WHERE tenant_id = %s", (context.tenant,)
                ).fetchone()[0]
            assert count == 0, table
        rows = connection.execute(
            "SELECT * FROM candidate_data_tombstones WHERE tenant_id = %s", (context.tenant,)
        )
        assert [column.name for column in rows.description] == [
            "tenant_id",
            "application_id",
            "role_id",
            "final_status",
            "deleted_at",
        ]
        row = rows.fetchone()
        assert str(row[1]) == result["application_id"]
        assert row[3] == "rejected"
        assert rows.fetchone() is None
    assert (
        context.client.get(
            f"{context.base}/applications/{result['application_id']}", headers=context.headers
        ).status_code
        == 404
    )
    # Deletion removed the identity/hash keys; a later upload can start a fresh workflow.
    assert context.upload(cv).status_code == 202


def test_inactive_default_and_disabled_deletion(
    context: Context, cv: bytes, admin_url: str
) -> None:
    response = context.upload(cv)
    context.drain()
    result = context.result(response.json()["upload_id"])
    with psycopg.connect(admin_url) as connection:
        activity, expiry = connection.execute(
            "SELECT last_activity_at, retention_expires_at FROM applications WHERE id = %s",
            (result["application_id"],),
        ).fetchone()
        assert expiry - activity == timedelta(days=30)
    expire(context, admin_url, result["application_id"])
    context.drain()
    assert (
        context.client.get(
            f"{context.base}/applications/{result['application_id']}", headers=context.headers
        ).status_code
        == 200
    )
    context.settings.auto_delete_enabled = True
    context.drain()
    assert (
        context.client.get(
            f"{context.base}/applications/{result['application_id']}", headers=context.headers
        ).status_code
        == 404
    )


def test_retention_setting_change_recalculates_deadlines(
    context: Context, cv: bytes, admin_url: str
) -> None:
    response = context.upload(cv)
    context.drain()
    result = context.result(response.json()["upload_id"])
    response = context.client.patch(
        f"{context.base}/retention-settings",
        headers=context.headers,
        json={"other_candidate_retention_days": 12},
    )
    assert response.status_code == 200
    context.drain()
    with psycopg.connect(admin_url) as connection:
        activity, expiry = connection.execute(
            "SELECT last_activity_at, retention_expires_at FROM applications WHERE id = %s",
            (result["application_id"],),
        ).fetchone()
        assert expiry - activity == timedelta(days=12)


def test_deletion_keeps_shared_identity_for_unexpired_reapplication(
    context: Context, cv: bytes, admin_url: str
) -> None:
    first = context.upload(cv)
    context.drain()
    old_result = context.result(first.json()["upload_id"])
    rejected = context.client.post(
        f"{context.base}/applications/{old_result['application_id']}/review-decisions",
        headers={**context.headers, "Idempotency-Key": str(uuid4())},
        json={
            "match_run_id": old_result["match_run_id"],
            "decision": "reject",
            "confirm_rejection": True,
            "reason": "Confirmed initial rejection.",
        },
    )
    assert rejected.status_code == 201
    second = context.upload(
        pdf_bytes(
            "Example Candidate\ncandidate@example.org\n+919876543210\n"
            "Python development with additional cloud experience."
        )
    )
    context.drain()
    new_result = context.result(second.json()["upload_id"])
    assert old_result["application_id"] != new_result["application_id"]
    assert new_result["recommendation"] == "needs_review"
    expire(context, admin_url, old_result["application_id"])
    context.settings.auto_delete_enabled = True
    context.drain()
    assert (
        context.client.get(
            f"{context.base}/applications/{new_result['application_id']}", headers=context.headers
        ).status_code
        == 200
    )
    with psycopg.connect(admin_url) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM candidate_contacts WHERE tenant_id = %s", (context.tenant,)
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM resume_files WHERE tenant_id = %s", (context.tenant,)
            ).fetchone()[0]
            == 1
        )
