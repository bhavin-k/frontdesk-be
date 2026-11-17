import io
import secrets
import time
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import pymupdf
from docx import Document
from fastapi.testclient import TestClient

from recruitment.adapters.providers import Providers
from recruitment.adapters.queue import PostgresJobQueue
from recruitment.core.config import Settings
from recruitment.jobs.runner import Worker


def pdf_bytes(text: str) -> bytes:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((40, 50), text, fontsize=10)
    result = document.tobytes()
    document.close()
    return result


def pdf_with_attachment(content: bytes, payload: bytes = b"hidden attachment fixture") -> bytes:
    with pymupdf.open(stream=content, filetype="pdf") as document:
        document.embfile_add("source.txt", payload)
        return bytes(document.tobytes())


def docx_bytes(text: str) -> bytes:
    document = Document()
    for line in text.splitlines():
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


@dataclass
class Context:
    client: TestClient
    tenant: str
    user_id: str
    headers: dict[str, str]
    opening: str
    settings: Settings
    providers: Providers

    @property
    def base(self) -> str:
        return f"/api/v1/tenants/{self.tenant}"

    def worker(self) -> Worker:
        return Worker(
            PostgresJobQueue(self.client.app.state.container.db, self.settings), self.providers
        )

    def upload(self, content: bytes, mime: str = "application/pdf", key: str | None = None) -> Any:
        return self.client.post(
            f"{self.base}/openings/{self.opening}/cv-uploads",
            headers={**self.headers, "Idempotency-Key": key or str(uuid4())},
            files={
                "file": (
                    "candidate.pdf" if mime == "application/pdf" else "candidate.docx",
                    content,
                    mime,
                )
            },
        )

    def drain(self) -> None:
        worker = self.worker()
        idle = 0
        for _ in range(60):
            if worker.run_once():
                idle = 0
            else:
                idle += 1
                if idle >= 3:
                    return
                time.sleep(0.03)
        raise AssertionError("Unexpected unbounded job processing.")

    def result(self, upload_id: str) -> dict[str, Any]:
        status = self.client.get(f"{self.base}/cv-uploads/{upload_id}", headers=self.headers).json()
        assert status["status"] == "completed", status
        response = self.client.get(
            f"{self.base}/applications/{status['application_id']}/match-results",
            headers=self.headers,
        )
        assert response.status_code == 200
        read = response.json()
        app = self.client.get(
            f"{self.base}/applications/{status['application_id']}", headers=self.headers
        ).json()
        evaluation = read["evaluation"]
        return {
            **(read["result"] or {}),
            "application_id": app["id"],
            "evaluation_id": evaluation["id"],
            "expected_version": app["version"],
            "recommendation": evaluation["recommendation"],
            "processed_at": evaluation["processed_at"],
        }


def identity(client: TestClient) -> tuple[str, dict[str, str]]:
    email = f"recruiter-{uuid4().hex}@example.org"
    credentials = {"email": email, "password": secrets.token_urlsafe(24)}
    registered = client.post("/api/v1/auth/users", json=credentials)
    assert registered.status_code == 201
    authenticated = client.post("/api/v1/auth/sessions", json=credentials)
    assert authenticated.status_code == 200
    return registered.json()["user_id"], {
        "Authorization": f"Bearer {authenticated.json()['access_token']}"
    }
