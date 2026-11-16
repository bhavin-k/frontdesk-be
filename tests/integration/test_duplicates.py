from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

from sqlalchemy import func, select

from recruitment.adapters.db.models import Application, Candidate, Resume, ResumeFile
from tests.support import Context, pdf_bytes


def test_repeated_and_concurrent_uploads_share_one_application(context: Context, cv: bytes) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: context.upload(cv), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 202]
    assert responses[0].json()["upload_id"] == responses[1].json()["upload_id"]
    context.drain()
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        for model in (Application, Candidate, ResumeFile):
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_idempotency_key_cannot_be_reused_for_different_file(context: Context, cv: bytes) -> None:
    key = str(uuid4())
    assert context.upload(cv, key=key).status_code == 202
    response = context.upload(
        pdf_bytes("Different Candidate\nPython development experience for four years."), key=key
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_changed_cv_versions_the_existing_application(context: Context, cv: bytes) -> None:
    first = context.upload(cv).json()
    context.drain()
    first_result = context.result(first["upload_id"])
    changed = context.upload(
        pdf_bytes(
            "Example Candidate\ncandidate@example.org\n+919876543210\n"
            "Python development: Built backend APIs for five years.\nPostgreSQL database design."
        )
    ).json()
    context.drain()
    second_result = context.result(changed["upload_id"])
    assert first_result["application_id"] == second_result["application_id"]
    assert second_result["recommendation"] == "needs_review"
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(func.count()).select_from(Application)) == 1
        assert sorted(session.scalars(select(Resume.version)).all()) == [1, 2]


def test_ambiguous_contact_identity_requires_review(context: Context) -> None:
    for email, phone in [
        ("first@example.org", "+919876543210"),
        ("second@example.org", "+919123456789"),
    ]:
        context.upload(pdf_bytes(f"Candidate\n{email}\n{phone}\nPython development experience."))
        context.drain()
    response = context.upload(
        pdf_bytes(
            "Conflicting Candidate\nfirst@example.org\n+919123456789\n"
            "Python development experience."
        )
    )
    context.drain()
    status = context.client.get(
        f"{context.base}/cv-uploads/{response.json()['upload_id']}", headers=context.headers
    ).json()
    assert status["status"] == "needs_review"
    assert status["review_reason"] == "POSSIBLE_DUPLICATE"
    assert status["application_id"] is None
