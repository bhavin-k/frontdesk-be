from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from recruitment.adapters.db.models import Application, Candidate, Opening
from recruitment.core.time import utcnow
from tests.support import Context, identity


def second_tenant(context: Context):
    user, headers = identity(context.client)
    response = context.client.post(
        "/api/v1/tenants", headers=headers, json={"name": "Second Business"}
    )
    assert response.status_code == 201
    return user, headers, response.json()["id"]


def test_all_recruiters_can_manage_members_and_retention(context: Context) -> None:
    user, headers = identity(context.client)
    response = context.client.post(
        f"{context.base}/members", headers=context.headers, json={"user_id": user}
    )
    assert response.status_code == 201
    assert response.json()["role"] == "recruiter"
    changed = context.client.patch(
        f"{context.base}/retention-settings",
        headers=headers,
        json={"rejected_retention_days": 9, "other_candidate_retention_days": 40},
    )
    assert changed.status_code == 200
    assert changed.json()["rejected_retention_days"] == 9
    removed = context.client.delete(f"{context.base}/members/{context.user_id}", headers=headers)
    assert removed.status_code == 204
    assert context.client.get(context.base, headers=context.headers).status_code == 404


def test_last_recruiter_cannot_be_removed(context: Context) -> None:
    response = context.client.delete(
        f"{context.base}/members/{context.user_id}", headers=context.headers
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "LAST_MEMBER"


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("GET", "", None),
        ("GET", "/members", None),
        ("POST", "/members", {"user_id": str(uuid4())}),
        ("DELETE", f"/members/{uuid4()}", None),
        ("GET", "/retention-settings", None),
        ("PATCH", "/retention-settings", {"rejected_retention_days": 8}),
        ("GET", "/review-queue", None),
        ("GET", "/openings", None),
        ("GET", "/cv-uploads", None),
        (
            "POST",
            "/openings",
            {
                "title": "Other opening",
                "job_description": "Build Python services.",
            },
        ),
    ],
)
def test_cross_tenant_endpoints_deny_reads_and_writes(
    context: Context, method: str, path: str, payload
) -> None:
    _, headers, _ = second_tenant(context)
    response = context.client.request(method, context.base + path, headers=headers, json=payload)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_cross_tenant_resource_identifiers_remain_inaccessible(context: Context, cv: bytes) -> None:
    upload = context.upload(cv).json()
    context.drain()
    result = context.result(upload["upload_id"])
    _, headers, tenant = second_tenant(context)
    other_base = f"/api/v1/tenants/{tenant}"
    for path in [
        f"/openings/{context.opening}",
        f"/cv-uploads/{upload['upload_id']}",
        f"/applications/{result['application_id']}",
        f"/applications/{result['application_id']}/match-results",
    ]:
        response = context.client.get(other_base + path, headers=headers)
        assert response.status_code == 404
    rejected = context.client.post(
        f"{other_base}/applications/{result['application_id']}/review-decisions",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        json={
            "match_run_id": result["match_run_id"],
            "decision": "reject",
            "confirm_rejection": True,
            "reason": "Unauthorized attempt.",
        },
    )
    assert rejected.status_code == 404
    response = context.client.post(
        f"{other_base}/openings/{context.opening}/cv-uploads",
        headers={**headers, "Idempotency-Key": str(uuid4())},
        files={"file": ("candidate.pdf", cv, "application/pdf")},
    )
    assert response.status_code == 404


def test_rls_checks_membership_and_clears_pooled_context(context: Context) -> None:
    other_user, _, other_tenant = second_tenant(context)
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(other_user)) as session:
        assert session.scalars(select(Opening)).all() == []
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert len(session.scalars(select(Opening)).all()) == 1
    with db.transaction(db.api) as session:
        assert session.scalars(select(Opening)).all() == []
    with db.transaction(db.worker, UUID(other_tenant)) as session:
        assert session.get(Opening, UUID(context.opening)) is None


def test_rls_prevents_forged_tenant_insert(context: Context) -> None:
    _, _, other_tenant = second_tenant(context)
    db = context.client.app.state.container.db
    with (
        pytest.raises(DBAPIError),
        db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session,
    ):
        session.add(Candidate(tenant_id=UUID(other_tenant), profile={}))
        session.flush()


def test_runtime_roles_cannot_bypass_boundaries(context: Context) -> None:
    db = context.client.app.state.container.db
    for engine, query in [
        (db.api, "SELECT password_hash FROM users"),
        (db.auth, "SELECT content FROM resume_files"),
        (db.dispatcher, "SELECT content FROM resume_files"),
        (db.worker, "INSERT INTO review_decisions DEFAULT VALUES"),
    ]:
        with (
            pytest.raises(DBAPIError),
            db.transaction(engine, UUID(context.tenant), UUID(context.user_id)) as session,
        ):
            session.execute(text(query))


def test_database_requires_a_human_rejection_decision(context: Context, cv: bytes) -> None:
    response = context.upload(cv)
    context.drain()
    result = context.result(response.json()["upload_id"])
    db = context.client.app.state.container.db
    for engine in (db.api, db.worker):
        with (
            pytest.raises(DBAPIError),
            db.transaction(engine, UUID(context.tenant), UUID(context.user_id)) as session,
        ):
            session.execute(
                update(Application)
                .where(Application.id == UUID(result["application_id"]))
                .values(status="rejected", rejected_at=utcnow())
            )


def test_unauthenticated_requests_are_rejected(context: Context) -> None:
    for path in (context.base, f"{context.base}/review-queue", "/api/v1/me"):
        response = context.client.get(path)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"


def test_only_recruiter_role_is_accepted(context: Context) -> None:
    response = context.client.post(
        f"{context.base}/members",
        headers=context.headers,
        json={"user_id": str(uuid4()), "role": "owner"},
    )
    assert response.status_code == 422
