import json
from uuid import UUID

import httpx
import pytest
from openai import OpenAI
from sqlalchemy import select

from recruitment.adapters.ai import LLMGrader
from recruitment.adapters.db.models import MatchRun
from recruitment.domain.contracts import EvidenceInput
from tests.support import pdf_bytes
from tests.unit.test_ai_configuration import chat_response


@pytest.mark.parametrize("recovers", [False, True])
def test_real_adapter_citation_repair_is_saved_or_routed_to_review(context, cv, recovers):
    fake = context.providers.grader
    requests = []

    def respond(request):
        body = json.loads(request.content)
        value = json.loads(body["messages"][1]["content"])
        requests.append(value)
        if "source_spans" in value:
            passage = next(item for item in value["source_spans"] if "Python" in item["text"])
            return httpx.Response(
                200,
                json=chat_response(
                    json.dumps(
                        {
                            "status": "met",
                            "confidence": 0.95,
                            "evidence_ids": [passage["id"] if recovers else "other-cv"],
                            "reason": "The selected passage describes Python experience.",
                        }
                    )
                ),
            )
        grade = fake.grade(
            value["criterion"], [EvidenceInput.model_validate(chunk) for chunk in value["chunks"]]
        )
        if not recovers or len(requests) == 1:
            grade.quotes = ["Invented candidate evidence"]
        return httpx.Response(200, json=chat_response(grade.model_dump_json()))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        context.providers.grader = LLMGrader(
            client, "test-model", "matching-v1", "chat_completions", "json_object"
        )
        uploaded = context.upload(cv).json()["upload_id"]
        context.drain()
    assert len(requests) == 2
    if recovers:
        result = context.result(uploaded)
        assert result["score"] == 100
        assert result["criteria"][0]["evidence"][0]["quote"] != "Invented candidate evidence"
        assert result["processing_config"]["evidence_validation_version"] == "exact-quote-v2"
    else:
        status = context.client.get(
            f"{context.base}/cv-uploads/{uploaded}", headers=context.headers
        ).json()
        assert status["status"] == "needs_review" and status["review_reason"] == "INVALID_EVIDENCE"
        db = context.client.app.state.container.db
        with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
            assert session.scalar(select(MatchRun.id)) is None


def test_whitespace_recovery_cannot_cite_through_redacted_personal_data(context):
    content = pdf_bytes(
        "Example Candidate\ncandidate@example.org\nPython\nAge: 34\nAPIs for three years."
    )

    def respond(request):
        value = json.loads(json.loads(request.content)["messages"][1]["content"])
        assert "Age: 34" not in json.dumps(value)
        grade = {
            "status": "met",
            "confidence": 0.99,
            "evidence_chunk_ids": [value["chunks"][0]["chunk_id"]],
            "quotes": ["Python APIs for three years."],
            "reason": "Fixture attempts to cross a redacted line.",
        }
        return httpx.Response(200, json=chat_response(json.dumps(grade)))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        context.providers.grader = LLMGrader(
            client, "test-model", "matching-v1", "chat_completions", "json_object"
        )
        uploaded = context.upload(content).json()["upload_id"]
        context.drain()
    status = context.client.get(
        f"{context.base}/cv-uploads/{uploaded}", headers=context.headers
    ).json()
    assert status["status"] == "needs_review" and status["review_reason"] == "INVALID_EVIDENCE"
    db = context.client.app.state.container.db
    with db.transaction(db.api, UUID(context.tenant), UUID(context.user_id)) as session:
        assert session.scalar(select(MatchRun.id)) is None
