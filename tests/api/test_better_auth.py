import secrets
from datetime import timedelta
from uuid import uuid4

import psycopg

from recruitment.core.time import utcnow


def web_identity(admin_url: str, expired: bool = False):
    user_id, session_id = uuid4(), uuid4()
    token = secrets.token_urlsafe(32)
    now = utcnow()
    expires = now - timedelta(seconds=1) if expired else now + timedelta(hours=1)
    with psycopg.connect(admin_url) as connection:
        connection.execute(
            "INSERT INTO users(id, created_at, updated_at, name, email, email_verified) "
            "VALUES (%s,%s,%s,%s,%s,true)",
            (user_id, now, now, "Web Recruiter", f"web-{uuid4().hex}@example.org"),
        )
        connection.execute(
            "INSERT INTO better_auth_sessions(id,user_id,token,expires_at,created_at,updated_at) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (session_id, user_id, token, expires, now, now),
        )
    return user_id, {"Authorization": f"Bearer {token}"}


def test_better_auth_session_creates_business_and_revokes_on_logout(client, admin_url: str) -> None:
    user_id, headers = web_identity(admin_url)
    me = client.get("/api/v1/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["user_id"] == str(user_id)
    assert "token" not in me.text
    created = client.post("/api/v1/tenants", headers=headers, json={"name": "Web Business"})
    assert created.status_code == 201
    assert created.json()["role"] == "recruiter"
    assert client.delete("/api/v1/auth/sessions/current", headers=headers).status_code == 204
    assert client.get("/api/v1/me", headers=headers).status_code == 401
    with psycopg.connect(admin_url) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM better_auth_sessions WHERE user_id = %s", (user_id,)
            ).fetchone()[0]
            == 0
        )


def test_expired_better_auth_session_is_rejected(client, admin_url: str) -> None:
    _, headers = web_identity(admin_url, expired=True)
    assert client.get("/api/v1/me", headers=headers).status_code == 401


def test_better_auth_only_user_cannot_use_legacy_password_login(client, admin_url: str) -> None:
    user_id, _ = web_identity(admin_url)
    with psycopg.connect(admin_url) as connection:
        email = connection.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()[
            0
        ]
    response = client.post(
        "/api/v1/auth/sessions",
        json={
            "email": email,
            "password": secrets.token_urlsafe(24),
        },
    )
    assert response.status_code == 401
