from tests.integration.test_authorization import second_tenant
from tests.support import Context


def test_workspace_lists_restore_records_with_pagination(context: Context, cv: bytes) -> None:
    upload = context.upload(cv).json()
    openings = context.client.get(f"{context.base}/openings", headers=context.headers)
    assert openings.status_code == 200
    assert openings.json()["items"][0]["id"] == context.opening
    assert openings.json()["items"][0]["criteria"][0]["text"] == "Python development"
    uploads = context.client.get(f"{context.base}/cv-uploads?limit=1", headers=context.headers)
    assert uploads.status_code == 200
    assert uploads.json()["items"][0]["upload_id"] == upload["upload_id"]
    assert (
        context.client.get(
            f"{context.base}/cv-uploads?limit=1&offset=1", headers=context.headers
        ).json()["items"]
        == []
    )
    context.drain()
    restored = context.client.get(f"{context.base}/cv-uploads", headers=context.headers).json()
    assert restored["items"][0]["application_id"] is not None
    assert restored["items"][0]["status"] == "completed"


def test_lists_are_authorized_and_tenant_scoped(context: Context, cv: bytes) -> None:
    context.upload(cv)
    _, other_headers, other_tenant = second_tenant(context)
    for resource in ("openings", "cv-uploads"):
        assert context.client.get(f"{context.base}/{resource}").status_code == 401
        assert (
            context.client.get(f"{context.base}/{resource}", headers=other_headers).status_code
            == 404
        )
        assert (
            context.client.get(
                f"/api/v1/tenants/{other_tenant}/{resource}", headers=other_headers
            ).json()["items"]
            == []
        )
        for query in ("limit=0", "limit=101", "offset=-1", "offset=10001"):
            assert (
                context.client.get(
                    f"{context.base}/{resource}?{query}", headers=context.headers
                ).status_code
                == 422
            )
