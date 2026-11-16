import json

import httpx
import pytest
from openai import OpenAI

from recruitment.adapters.experience import (
    EXPERIENCE_PROMPT_VERSION,
    CompactExperienceRequest,
    FakeExperience,
    LLMExperience,
)
from recruitment.core.errors import ProviderError
from recruitment.domain.experience import ExperienceFacts, ExperienceSource, validate_facts
from recruitment.services.matching import opening_experience_sources, source_lines
from tests.unit.test_ai_configuration import chat_response


@pytest.mark.parametrize("generation_error", [False, True])
def test_real_experience_adapter_uses_shared_model_and_source_identifiers(generation_error):
    sources = [
        *source_lines("cv:4", "\n\nEngineer 2024-01 to Present Python", page=4),
        *source_lines("role:job", "\n2 years experience", opening_id="role"),
    ]
    expected = FakeExperience().extract(sources, ["role"])
    compact = FakeExperience().extract(
        [
            ExperienceSource(id="s1", text=sources[0].text),
            ExperienceSource(id="s2", text=sources[1].text, opening_id="r1"),
        ],
        ["r1"],
    )
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["model"] == "configured-profile-model"
        payload = json.loads(body["messages"][1]["content"])
        assert payload == {
            "cv": [["s1", sources[0].text]],
            "openings": [{"id": "r1", "sources": [["s2", sources[1].text]]}],
        }
        assert '"properties"' not in body["messages"][0]["content"]
        assert body["response_format"]["type"] == "json_schema"
        if generation_error and len(calls) == 1:
            return httpx.Response(400, json={"error": {"code": "json_validate_failed"}})
        return httpx.Response(200, json=chat_response(compact.model_dump_json()))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        provider = LLMExperience(
            client,
            "configured-profile-model",
            EXPERIENCE_PROMPT_VERSION,
            "chat_completions",
            "json_schema",
        )
        facts = provider.extract(sources, ["role"])
    validate_facts(facts, sources, ["role"])
    assert facts == expected
    assert facts.employment[0].evidence_ids == [sources[0].id]
    assert sources[0].page == 4 and sources[0].char_start == 2
    assert len(calls) == (2 if generation_error else 1)


def test_compact_sources_reject_unknown_ids_and_keep_scope_validation():
    sources = [
        ExperienceSource(id="cv-original", text="Engineer 2024-01 to Present Python"),
        ExperienceSource(id="jd-original", text="2 years experience", opening_id="opening"),
    ]
    request = CompactExperienceRequest.build(sources, ["opening"])
    facts = ExperienceFacts.model_validate(
        {
            "roles": [{"opening_id": "r1", "requirements": []}],
            "employment": [],
            "complete_employment": False,
        }
    )
    facts.roles[0].opening_id = "missing"
    with pytest.raises(ProviderError, match="INVALID_EXPERIENCE_EVIDENCE"):
        request.resolve(facts)
    quoted = FakeExperience().extract(
        [
            ExperienceSource(id="s1", text=sources[0].text),
            ExperienceSource(id="s2", text=sources[1].text, opening_id="r1"),
        ],
        ["r1"],
    )
    quoted.employment[0].evidence_ids = ["s3"]
    with pytest.raises(ProviderError, match="INVALID_EXPERIENCE_EVIDENCE"):
        request.resolve(quoted)
    quoted.employment[0].evidence_ids = ["s2"]
    with pytest.raises(ProviderError, match="INVALID_EXPERIENCE_EVIDENCE"):
        validate_facts(request.resolve(quoted), sources, ["opening"])
    quoted.employment[0].evidence_ids = ["s1"]
    quoted.employment[0].start_month = "2024-02"
    with pytest.raises(ProviderError, match="INVALID_EXPERIENCE_EVIDENCE"):
        validate_facts(request.resolve(quoted), sources, ["opening"])


def test_job_evidence_keeps_minimums_and_verified_criteria_with_offsets():
    document = (
        "Company history unrelated to the role\n\n"
        "Core platform work\n"
        "Build and operate Python APIs.\n"
        "Responsibilities include reviewing operational logs.\n\n"
        "Unrelated office information\n"
        "A plain context line\n"
        "Required professional experience\n"
        "2 years in a regulated industry.\n"
        "Comparable domain work is also accepted.\n"
        "Unrelated event information"
    )
    kept = opening_experience_sources(
        "job:document", document, "opening", ["Build and operate Python APIs."]
    )
    text = {item.text for item in kept}
    assert {
        "Build and operate Python APIs.",
        "Required professional experience",
        "2 years in a regulated industry.",
        "Comparable domain work is also accepted.",
    } <= text
    assert "Company history unrelated to the role" not in text
    assert "Unrelated event information" not in text
    assert all(item.text == document[item.char_start : item.char_end] for item in kept)
    original = source_lines("job:document", document, opening_id="opening")
    assert opening_experience_sources("job:document", document, "opening", None) == original
    assert opening_experience_sources("job:document", document, "opening", ["missing"]) == original


def test_experience_request_budget_checks_complete_request_before_transport():
    sources = [
        ExperienceSource(id="source-original", text="Synthetic role context", opening_id="role")
    ]
    request = CompactExperienceRequest.build(sources, ["role"])
    calls = []

    def respond(http_request):
        calls.append(json.loads(http_request.content))
        response = {
            "roles": [{"opening_id": "r1", "requirements": []}],
            "employment": [],
            "complete_employment": False,
        }
        return httpx.Response(200, json=chat_response(json.dumps(response)))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        provider = LLMExperience(
            client,
            "test-model",
            EXPERIENCE_PROMPT_VERSION,
            "chat_completions",
            "json_schema",
            output_tokens=256,
        )
        exact = provider.estimate_input_tokens(request.value, ExperienceFacts, "") + 256
        provider.request_tokens = exact - 1
        with pytest.raises(ProviderError, match="EXPERIENCE_REQUEST_BUDGET_EXCEEDED") as caught:
            provider.extract(sources, ["role"])
        assert not caught.value.retryable and not calls
        provider.request_tokens = exact
        facts = provider.extract(sources, ["role"])
    assert facts.roles[0].opening_id == "role"
    assert len(calls) == 1 and calls[0]["max_completion_tokens"] == 256


@pytest.mark.parametrize(
    "status,provider_code,safe_code",
    [
        (413, "rate_limit_exceeded", "rate_limit_exceeded"),
        (429, "SENSITIVE_BODY_MARKER", "unclassified"),
    ],
)
def test_eligibility_provider_logs_safe_error_metadata(status, provider_code, safe_code, caplog):
    def respond(_request):
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": provider_code,
                    "message": "SENSITIVE_BODY_MARKER",
                }
            },
        )

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        provider = LLMExperience(
            client,
            "test-model",
            EXPERIENCE_PROMPT_VERSION,
            "chat_completions",
            "json_schema",
        )
        with pytest.raises(ProviderError):
            provider.parse("SENSITIVE_BODY_MARKER", ExperienceFacts, max_attempts=1)
    assert f"status_code={status} provider_code={safe_code}" in caplog.text
    assert "SENSITIVE_BODY_MARKER" not in caplog.text
