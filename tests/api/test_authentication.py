import secrets
from uuid import uuid4

import psycopg

from tests.support import Context


def test_login_logout_and_session_hash_storage(client, admin_url: str) -> None:
    credentials = {
        "email": f"account-{uuid4().hex}@example.org",
        "password": secrets.token_urlsafe(24),
    }
    assert client.post("/api/v1/auth/users", json=credentials).status_code == 201
    login = client.post("/api/v1/auth/sessions", json=credentials)
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/me", headers=headers).status_code == 200
    with psycopg.connect(admin_url) as connection:
        password_hash = connection.execute("SELECT password_hash FROM users").fetchone()[0]
        token_hash = connection.execute("SELECT token_hash FROM auth_sessions").fetchone()[0]
        assert password_hash.startswith("$argon2id$")
        assert token not in token_hash and credentials["password"] not in password_hash
    assert client.delete("/api/v1/auth/sessions/current", headers=headers).status_code == 204
    assert client.get("/api/v1/me", headers=headers).status_code == 401


def test_login_rate_limit_counts_failed_attempts(client) -> None:
    credentials = {
        "email": f"missing-{uuid4().hex}@example.org",
        "password": secrets.token_urlsafe(24),
    }
    for _ in range(10):
        assert client.post("/api/v1/auth/sessions", json=credentials).status_code == 401
    assert client.post("/api/v1/auth/sessions", json=credentials).status_code == 429


def test_validation_errors_never_echo_passwords(client) -> None:
    password = "secret123"
    response = client.post(
        "/api/v1/auth/users",
        json={
            "email": "valid@example.org",
            "password": password,
        },
    )
    assert response.status_code == 422
    assert password not in response.text
    assert "input" not in response.text
    assert response.headers["Cache-Control"] == "no-store"


def test_openapi_health_and_multiple_active_openings(context: Context) -> None:
    assert context.client.get("/health/live").json() == {"status": "ok"}
    assert context.client.get("/health/ready").json() == {"status": "ok"}
    schema = context.client.get("/openapi.json").json()
    assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
    paths = schema["paths"]
    assert "/api/v1/tenants/{tenant_id}/openings/{opening_id}/cv-uploads" in paths
    creation = paths["/api/v1/tenants/{tenant_id}/openings"]["post"]["requestBody"]["content"]
    assert set(creation) == {"application/json", "multipart/form-data"}
    assert creation["application/json"]["schema"]["required"] == ["title", "job_description"]
    assert "JobDetails" in schema["components"]["schemas"]
    assert not any("sarvam" in path or "whatsapp" in path for path in paths)
    response = context.client.post(
        f"{context.base}/openings",
        headers=context.headers,
        json={
            "title": "Second opening",
            "job_description": "Another Python opening.",
        },
    )
    assert response.status_code == 201


def test_manual_scoring_inputs_are_rejected_at_api_boundary(context: Context, client) -> None:
    tenant = client.post(
        "/api/v1/tenants", headers=context.headers, json={"name": "Another business"}
    ).json()["id"]
    response = client.post(
        f"/api/v1/tenants/{tenant}/openings",
        headers=context.headers,
        json={
            "title": "Disallowed opening",
            "job_description": "Python development role.",
            "criteria": [{"text": "Male under 30 years of age"}],
        },
    )
    assert response.status_code == 422
    assert "criteria" in str(response.json()["error"]["details"])
