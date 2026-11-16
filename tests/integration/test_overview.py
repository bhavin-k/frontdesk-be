from uuid import uuid4

from tests.support import Context, identity, pdf_bytes


def test_overview_counts_retained_cvs_and_confirmed_decisions(context: Context, cv: bytes) -> None:
    empty = context.client.get(f"{context.base}/overview", headers=context.headers)
    assert empty.status_code == 200
    assert set(empty.json()["totals"].values()) == {0}
    first = context.upload(cv).json()
    assert context.upload(cv).status_code == 200
    context.drain()
    result = context.result(first["upload_id"])
    summary = context.client.get(f"{context.base}/overview", headers=context.headers).json()
    assert summary["totals"] == {
        "cvs_received": 1,
        "processed": 1,
        "shortlisted": 0,
        "needs_review": 1,
        "rejected": 0,
    }
    assert sum(row["cvs_received"] for row in summary["daily_intake"]) == 1
    assert summary["scope"] == "retained_records"
    assert summary["timezone"] == "Asia/Kolkata"
    rejected = context.client.post(
        f"{context.base}/applications/{result['application_id']}/review-decisions",
        headers={**context.headers, "Idempotency-Key": str(uuid4())},
        json={
            "match_run_id": result["match_run_id"],
            "decision": "reject",
            "confirm_rejection": True,
            "reason": "Reviewed the supporting CV evidence.",
        },
    )
    assert rejected.status_code == 201
    updated = context.client.get(f"{context.base}/overview", headers=context.headers).json()
    assert updated["totals"]["rejected"] == 1
    assert updated["totals"]["shortlisted"] == 0


def test_overview_needs_review_and_opening_scope(context: Context) -> None:
    context.upload(pdf_bytes("Candidate\nunknown@example.org\nUnrelated warehouse experience."))
    context.drain()
    summary = context.client.get(
        f"{context.base}/overview?opening_id={context.opening}&days=7", headers=context.headers
    )
    assert summary.status_code == 200
    assert summary.json()["totals"]["needs_review"] == 1
    assert len(summary.json()["daily_intake"]) == 7
    assert summary.json()["by_opening"][0]["opening_id"] == context.opening
    assert (
        context.client.get(
            f"{context.base}/overview?opening_id={uuid4()}", headers=context.headers
        ).status_code
        == 404
    )
    assert (
        context.client.get(f"{context.base}/overview?days=0", headers=context.headers).status_code
        == 422
    )


def test_overview_cannot_read_another_business(context: Context, cv: bytes) -> None:
    context.upload(cv)
    context.drain()
    _, stranger = identity(context.client)
    assert context.client.get(f"{context.base}/overview").status_code == 401
    assert context.client.get(f"{context.base}/overview", headers=stranger).status_code == 404
    tenant = context.client.post(
        "/api/v1/tenants", headers=stranger, json={"name": "Other business"}
    ).json()["id"]
    response = context.client.get(f"/api/v1/tenants/{tenant}/overview", headers=stranger)
    assert response.status_code == 200
    assert set(response.json()["totals"].values()) == {0}
