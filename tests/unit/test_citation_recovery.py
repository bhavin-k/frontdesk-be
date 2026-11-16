import json

import httpx
import pytest
from openai import OpenAI

from recruitment.adapters.ai import LLMGrader
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import EvidenceInput
from recruitment.domain.rag import token_count
from tests.unit.test_ai_configuration import chat_response


def result(quote="Python APIs", status="met"):
    return {
        "status": status,
        "confidence": 0.95,
        "evidence_chunk_ids": ["chunk-1"],
        "quotes": [quote],
        "reason": "The source describes the requirement.",
    }


def selection(ids=None, status="met"):
    return {
        "status": status,
        "confidence": 0.95,
        "evidence_ids": ["s1"] if ids is None else ids,
        "reason": "The selected source describes the requirement.",
    }


@pytest.mark.parametrize("mode", ["text", "json_object", "json_schema"])
def test_grader_requests_one_source_bound_correction(mode, caplog):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        value = result("Invented experience") if len(requests) == 1 else selection()
        return httpx.Response(200, json=chat_response(json.dumps(value)))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        value = LLMGrader(client, "test-model", "matching-v1", "chat_completions", mode).grade(
            "Python development", [EvidenceInput(chunk_id="chunk-1", text="Python APIs")]
        )
    assert value.quotes == ["Python APIs"] and len(requests) == 2
    retry = json.loads(requests[1]["messages"][1]["content"])
    assert retry["citation_correction"]["validation_error"] == "QUOTE_NOT_FOUND"
    assert retry["citation_rules"]["allowed_chunk_ids"] == ["chunk-1"]
    assert retry["source_spans"] == [{"id": "s1", "chunk_id": "chunk-1", "text": "Python APIs"}]
    assert "chunks" not in retry and "previous_response" not in retry["citation_correction"]
    assert "Invented experience" not in caplog.text and "Python APIs" not in caplog.text


def test_whitespace_only_quotes_need_no_extra_model_request():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=chat_response(json.dumps(result())))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        value = LLMGrader(client, "test-model", "matching-v1", "chat_completions", "text").grade(
            "Python development", [EvidenceInput(chunk_id="chunk-1", text="Python\nAPIs")]
        )
    assert value.quotes == ["Python\nAPIs"] and len(calls) == 1


def test_unknown_without_evidence_uses_empty_arrays_after_correction():
    calls = []

    def respond(request):
        calls.append(request)
        value = result("Not stated in the CV", "unknown")
        if len(calls) == 2:
            value = selection([], "unknown")
        return httpx.Response(200, json=chat_response(json.dumps(value)))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        value = LLMGrader(client, "test-model", "matching-v1", "chat_completions", "text").grade(
            "Communication", [EvidenceInput(chunk_id="chunk-1", text="Python APIs")]
        )
    assert value.status == "unknown" and value.quotes == []
    assert value.evidence_chunk_ids == [] and len(calls) == 2


@pytest.mark.parametrize("bad", ["wrong_quote", "wrong_chunk", "counts", "schema"])
def test_bad_citations_fail_closed_after_exactly_one_correction(bad):
    calls = []

    def respond(request):
        calls.append(request)
        value = result("Unsupported quote")
        if bad == "wrong_chunk":
            value = {**result(), "evidence_chunk_ids": ["other-candidate"]}
        elif bad == "counts":
            value["evidence_chunk_ids"] = []
        elif bad == "schema":
            value = {}
        return httpx.Response(200, json=chat_response(json.dumps(value)))

    with (
        OpenAI(
            api_key="local-mock-only",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client,
        pytest.raises(ProviderError) as caught,
    ):
        LLMGrader(client, "test-model", "matching-v1", "chat_completions", "text").grade(
            "Python development", [EvidenceInput(chunk_id="chunk-1", text="Python APIs")]
        )
    assert caught.value.code in {"INVALID_EVIDENCE", "INVALID_MODEL_OUTPUT"}
    assert len(calls) == 2


@pytest.mark.parametrize("always_limited", [False, True])
def test_grading_rate_limits_respect_retry_after_and_remain_bounded(monkeypatch, always_limited):
    calls, delays = [], []
    monkeypatch.setattr("recruitment.adapters.ai.time.sleep", delays.append)

    def respond(request):
        calls.append(request)
        if always_limited or len(calls) == 1:
            return httpx.Response(
                429,
                headers={"retry-after": "2"},
                json={"error": {"message": "Fixture rate limit", "type": "rate_limit_error"}},
            )
        return httpx.Response(200, json=chat_response(json.dumps(result())))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        grader = LLMGrader(client, "test-model", "matching-v1", "chat_completions", "text")
        if always_limited:
            with pytest.raises(ProviderError) as caught:
                grader.grade("Python", [EvidenceInput(chunk_id="chunk-1", text="Python APIs")])
            assert caught.value.code == "AI_UNAVAILABLE" and caught.value.retryable
            assert len(calls) == 3 and delays == [2.25, 2.25]
        else:
            assert grader.grade(
                "Python", [EvidenceInput(chunk_id="chunk-1", text="Python APIs")]
            ).quotes == ["Python APIs"]
            assert len(calls) == 2 and delays == [2.25]


@pytest.mark.parametrize("always_invalid", [False, True])
def test_provider_json_generation_errors_use_the_same_bounded_correction(always_invalid):
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        if always_invalid or len(calls) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {"code": "json_validate_failed", "message": "Fixture generation error"}
                },
            )
        return httpx.Response(200, json=chat_response(json.dumps(selection())))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        grader = LLMGrader(client, "test-model", "matching-v1", "chat_completions", "json_schema")
        evidence = [EvidenceInput(chunk_id="chunk-1", text="Python APIs")]
        if always_invalid:
            with pytest.raises(ProviderError, match="INVALID_MODEL_OUTPUT"):
                grader.grade("Python", evidence)
        else:
            assert grader.grade("Python", evidence).quotes == ["Python APIs"]
    assert len(calls) == 2
    correction = json.loads(calls[1]["messages"][1]["content"])["citation_correction"]
    assert correction["validation_error"] == "INVALID_MODEL_OUTPUT"


def test_unrelated_bad_requests_are_not_retried_or_treated_as_output_errors(caplog):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            400,
            json={"error": {"code": "model_not_found", "message": "Private provider error"}},
        )

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        with pytest.raises(ProviderError) as caught:
            LLMGrader(client, "test-model", "matching-v1", "chat_completions", "json_schema").grade(
                "Python", [EvidenceInput(chunk_id="chunk-1", text="Python APIs")]
            )
        assert caught.value.code == "AI_REQUEST_FAILED" and not caught.value.retryable
    assert len(calls) == 1
    assert "ai.request_failed status_code=400" in caplog.text
    assert "Private provider error" not in caplog.text


@pytest.mark.parametrize("budget", [30, 300])
def test_correction_passages_stay_within_the_evidence_budget(budget):
    calls = []
    chunks = [
        EvidenceInput(chunk_id=f"c{i}", text="Python APIs " * 30 + "\nSQL") for i in range(12)
    ]

    def respond(request):
        payload = json.loads(json.loads(request.content)["messages"][1]["content"])
        calls.append(payload)
        if len(calls) == 1:
            value = result("Invented claim")
        else:
            assert token_count(json.dumps(payload["source_spans"])) <= budget
            assert all(
                any(
                    item["chunk_id"] == chunk.chunk_id and item["text"] in chunk.text
                    for chunk in chunks
                )
                for item in payload["source_spans"]
            )
            value = selection([], "unknown")
        return httpx.Response(200, json=chat_response(json.dumps(value)))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        grade = LLMGrader(
            client, "test-model", "matching-v1", "chat_completions", "text", evidence_budget=budget
        ).grade("Python", chunks)
    assert grade.status == "unknown" and len(calls) == 2
