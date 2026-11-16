from datetime import datetime, timedelta
from uuid import uuid4

from tests.support import Context


def test_rejection_requires_confirmation_and_sets_retention(context: Context, cv: bytes) -> None:
    response = context.upload(cv)
    context.drain()
    result = context.result(response.json()["upload_id"])
    path = f"{context.base}/applications/{result['application_id']}/review-decisions"
    payload = {
        "match_run_id": result["match_run_id"],
        "decision": "reject",
        "confirm_rejection": False,
        "reason": "Recruiter reviewed the candidate evidence.",
    }
    headers = {**context.headers, "Idempotency-Key": str(uuid4())}
    assert context.client.post(path, headers=headers, json=payload).status_code == 422
    before = context.client.get(
        f"{context.base}/applications/{result['application_id']}", headers=context.headers
    ).json()
    assert before["status"] != "rejected"
    payload["confirm_rejection"] = True
    decision = context.client.post(path, headers=headers, json=payload)
    assert decision.status_code == 201
    assert decision.json()["status"] == "rejected"
    expiry = datetime.fromisoformat(decision.json()["retention_expires_at"].replace("Z", "+00:00"))
    created = datetime.fromisoformat(result["processed_at"].replace("Z", "+00:00"))
    assert timedelta(days=7) <= expiry - created < timedelta(days=7, minutes=1)
    repeated = context.client.post(path, headers=headers, json=payload)
    assert repeated.status_code == 200
    assert repeated.json()["id"] == decision.json()["id"]
    assert (
        context.client.post(
            path, headers={**context.headers, "Idempotency-Key": str(uuid4())}, json=payload
        ).status_code
        == 409
    )


def test_stale_or_forged_match_cannot_be_confirmed(context: Context, cv: bytes) -> None:
    response = context.upload(cv)
    context.drain()
    result = context.result(response.json()["upload_id"])
    decision = context.client.post(
        f"{context.base}/applications/{result['application_id']}/review-decisions",
        headers={**context.headers, "Idempotency-Key": str(uuid4())},
        json={
            "match_run_id": str(uuid4()),
            "decision": "reject",
            "confirm_rejection": True,
            "reason": "Attempt with a stale result.",
        },
    )
    assert decision.status_code == 409
