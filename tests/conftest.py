import json
import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic.config import Config
from fastapi.testclient import TestClient

from alembic import command
from recruitment.adapters.db.models import Base
from recruitment.adapters.providers import Providers
from recruitment.api.app import create_app
from recruitment.core.config import Settings
from tests.support import Context, identity, pdf_bytes

ROOT = Path(__file__).resolve().parents[1]


class HiddenURL(str):
    def __repr__(self) -> str:
        return "<dedicated-test-database>"


state = ROOT / ".local" / "test-database.json"
if state.exists():
    for key, value in json.loads(state.read_text("utf-8"))["environment"].items():
        os.environ.setdefault(key, value)


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = socket.socket.connect

    def blocked(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Automated tests must not contact external services.")

    def local_connect(sock: socket.socket, address: Any) -> Any:
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
            blocked()
        return connect(sock, address)

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", local_connect)


@pytest.fixture(scope="session")
def admin_url() -> str:
    value = os.environ.get("RECRUITMENT_TEST_ADMIN_URL")
    if not value:
        pytest.skip("A dedicated RECRUITMENT_TEST_ADMIN_URL is required.")
    with psycopg.connect(value) as connection:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        if name != "recruitment_phase1_test":
            raise RuntimeError("Tests refuse any database except recruitment_phase1_test.")
    return HiddenURL(value)


@pytest.fixture(scope="session")
def migrated(admin_url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    command.upgrade(config, "head")


@pytest.fixture
def clean_database(migrated: None, admin_url: str) -> None:
    with psycopg.connect(admin_url) as connection:
        tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
        connection.execute(f"TRUNCATE {tables} CASCADE")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        provider_mode="fake",
        scoring_strategy="rag",
        mailbox_provider_mode="fake",
        auto_delete_enabled=False,
    )


@pytest.fixture
def providers(settings: Settings) -> Providers:
    return Providers.from_settings(settings)


@pytest.fixture
def client(clean_database: None, settings: Settings, providers: Providers) -> Iterator[TestClient]:
    with TestClient(create_app(settings, providers)) as value:
        yield value


@pytest.fixture
def context(client: TestClient, settings: Settings, providers: Providers) -> Context:
    user_id, headers = identity(client)
    tenant_response = client.post(
        "/api/v1/tenants", headers=headers, json={"name": "Example Recruitment"}
    )
    assert tenant_response.status_code == 201
    tenant = tenant_response.json()["id"]
    opening_response = client.post(
        f"/api/v1/tenants/{tenant}/openings",
        headers=headers,
        json={
            "title": "Python Developer",
            "job_description": "Python development",
        },
    )
    assert opening_response.status_code == 201
    context = Context(
        client, tenant, user_id, headers, opening_response.json()["id"], settings, providers
    )
    context.drain()
    return context


@pytest.fixture
def cv() -> bytes:
    return pdf_bytes(
        "Example Candidate\ncandidate@example.org\n+919876543210\n"
        "Python development: Built backend APIs for three years.\n"
        "PostgreSQL: Designed database schemas and migrations.\n"
        "Education: Bachelor of Engineering."
    )
