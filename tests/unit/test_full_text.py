import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from uuid import uuid4

import httpx
import pytest
from openai import OpenAI

from recruitment.adapters.ai_requests import retry_after_seconds
from recruitment.adapters.full_text import LLMFullTextGrader
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import Criterion, Page
from recruitment.domain.full_text import (
    FULL_TEXT_PROMPT_VERSION,
    FullTextDocument,
    FullTextResponse,
    FullTextRubric,
    RubricCriterion,
    rubric_prefix,
    verify_full_text,
)
from recruitment.domain.matching import calculate_verified_match
from tests.unit.test_citation_recovery import chat_response


@pytest.fixture
def rubric():
    return FullTextRubric(
        uuid4(),
        uuid4(),
        1,
        uuid4(),
        (
            RubricCriterion("c1", "Python APIs", True, False),
            RubricCriterion("c2", "Database migrations", True, False),
        ),
    )


def grades(quote="Python APIs"):
    return [
        {
            "criterion_id": "c1",
            "status": "met",
            "confidence": 0.95,
            "reason": "Relevant API experience.",
            "citations": [{"source_id": "s1", "quote": quote}],
        },
        {
            "criterion_id": "c2",
            "status": "unknown",
            "confidence": 0.95,
            "reason": "No conclusive evidence.",
            "citations": [],
        },
    ]


@pytest.mark.parametrize("number,kind", [(4, "page"), (None, "document")])
def test_exact_character_offsets_on_pdf_and_docx(rubric, number, kind):
    source = "Example Person\nDéveloppé Python APIs."
    document = FullTextDocument.from_pages([Page(number=number, text=source)], ["Example Person"])
    response = FullTextResponse.model_validate({"grades": list(reversed(grades()))})
    result = verify_full_text(rubric, document, response)
    assert [grade.criterion_id for grade in result.grades] == ["c1", "c2"]
    evidence = result.evidence["c1"][0]
    assert (evidence.source_kind, evidence.page) == (kind, number)
    assert evidence.char_start == source.index("Python")
    assert source[evidence.char_start : evidence.char_end] == evidence.quote == "Python APIs"
    assert "Example Person" not in document.request()


def test_line_breaks_resolve_to_original_offsets(rubric):
    source = "Developed Python\n  APIs with testing."
    document = FullTextDocument.from_pages([Page(number=1, text=source)], [])
    response = FullTextResponse.model_validate({"grades": grades()})

    evidence = verify_full_text(rubric, document, response).evidence["c1"][0]

    assert evidence.char_start == source.index("Python")
    assert source[evidence.char_start : evidence.char_end] == evidence.quote == "Python\n  APIs"


def test_missing_grade_can_omit_a_quote(rubric):
    entries = grades()
    entries[1]["status"] = "missing"
    document = FullTextDocument.from_pages([Page(number=1, text="Python APIs")], [])
    response = FullTextResponse.model_validate({"grades": entries})

    result = verify_full_text(rubric, document, response)

    assert result.grades[1].status == "missing"
    assert result.evidence["c2"] == []


@pytest.mark.parametrize("ids", [["c1"], ["c1", "c1"], ["c1", "c3"]])
def test_full_criterion_coverage_is_required(rubric, ids):
    document = FullTextDocument.from_pages([Page(number=1, text="Python APIs")], [])
    entries = [
        dict(value, criterion_id=identifier)
        for value, identifier in zip(grades(), ids, strict=False)
    ]
    response = FullTextResponse.model_validate({"grades": entries})
    with pytest.raises(ProviderError, match="INVALID_CRITERION_COVERAGE"):
        verify_full_text(rubric, document, response)


@pytest.mark.parametrize("quote", ["Fabricated passage", "Python        APIs", "Secret"])
def test_quotes_must_match_original_and_visible_text(rubric, quote):
    document = FullTextDocument.from_pages([Page(number=1, text="Python Secret APIs")], ["Secret"])
    response = FullTextResponse.model_validate({"grades": grades(quote)})
    with pytest.raises(ProviderError, match="INVALID_EVIDENCE"):
        verify_full_text(rubric, document, response)


@pytest.mark.parametrize("invalid", ["quote", "schema", "always"])
def test_repair_is_one_request_and_keeps_cv_last(rubric, invalid):
    requests = []
    document = FullTextDocument.from_pages(
        [Page(number=1, text="Python APIs and database migrations")], []
    )

    def respond(request):
        requests.append(json.loads(request.content))
        entries = grades(
            "Fabricated" if invalid == "always" or len(requests) == 1 else "Python APIs"
        )
        entries[1].update(
            status="met", citations=[{"source_id": "s1", "quote": "database migrations"}]
        )
        if invalid == "schema" and len(requests) == 1:
            entries[0]["confidence"] = "0.95"
        return httpx.Response(200, json=chat_response(json.dumps({"grades": entries})))

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        adapter = LLMFullTextGrader(
            client, "test-model", FULL_TEXT_PROMPT_VERSION, "chat_completions", "text"
        )
        result = adapter.grade(rubric, document)
        if invalid == "always":
            assert [grade.status for grade in result.grades] == ["unknown", "met"]
            assert result.grades[0].citations == []
            assert result.evidence["c1"] == []
            assert result.evidence["c2"][0].quote == "database migrations"
            criteria = [
                Criterion(text=entry.text, required=entry.required, strict=entry.strict, weight=1)
                for entry in rubric.criteria
            ]
            score = calculate_verified_match(criteria, result.grades, Decimal(75))
            assert score.recommendation == "needs_review"
            assert score.score == Decimal(50)
        else:
            assert result.evidence["c1"][0].quote == "Python APIs"

    assert len(requests) == 2
    for request in requests:
        assert rubric_prefix(rubric) in request["messages"][0]["content"]
        assert request["messages"][-1]["content"] == document.request()
    assert "Repair the previous response" in requests[1]["messages"][0]["content"]


def test_rubric_cache_is_scoped_to_tenant_and_revision(rubric):
    before = rubric_prefix.cache_info()
    rubric_prefix(rubric)
    rubric_prefix(rubric)
    rubric_prefix(replace(rubric, tenant_id=uuid4()))
    rubric_prefix(replace(rubric, source_revision=2))
    after = rubric_prefix.cache_info()
    assert after.hits - before.hits == 1
    assert after.misses - before.misses == 3


def test_budget_includes_output_reserve_and_logs_only_usage(rubric, caplog):
    document = FullTextDocument.from_pages(
        [Page(number=1, text="Private fictional heading\nPython APIs")], [], cv_id=uuid4()
    )
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        response = chat_response(json.dumps({"grades": grades()}))
        response["usage"] = {
            "prompt_tokens": 321,
            "completion_tokens": 52,
            "total_tokens": 373,
            "prompt_tokens_details": {"cached_tokens": 128},
        }
        return httpx.Response(200, json=response)

    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        adapter = LLMFullTextGrader(
            client,
            "test-model",
            FULL_TEXT_PROMPT_VERSION,
            "chat_completions",
            "text",
            output_tokens=256,
        )
        boundary = (
            adapter.estimate_input_tokens(
                document.request(), FullTextResponse, rubric_prefix(rubric)
            )
            + 256
        )
        adapter.request_tokens = boundary - 1
        with caplog.at_level(logging.INFO):
            with pytest.raises(ProviderError, match="FULL_TEXT_REQUEST_BUDGET_EXCEEDED"):
                adapter.grade(rubric, document)
            assert not requests
            adapter.request_tokens = boundary
            adapter.grade(rubric, document)
    assert len(requests) == 1 and requests[0]["max_completion_tokens"] == 256
    logs = "\n".join(record.message for record in caplog.records)
    assert "outcome=FULL_TEXT_REQUEST_BUDGET_EXCEEDED" in logs
    assert "input_tokens=321 output_tokens=52 cached_tokens=128" in logs
    assert "attempts=1 outcome=success" in logs
    assert "Private fictional heading" not in logs and "Python APIs" not in logs


def test_retry_after_accepts_http_date_and_defers_long_delays(rubric):
    retry_at = format_datetime(datetime.now(UTC) + timedelta(seconds=125), usegmt=True)
    headers = httpx.Headers({"Retry-After": retry_at})
    assert 120 < retry_after_seconds(headers, fallback=1) <= 125
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429, headers=headers, json={"error": {"message": "Fixture limit"}})

    document = FullTextDocument.from_pages([Page(number=1, text="Python APIs")], [])
    with OpenAI(
        api_key="local-mock-only",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        adapter = LLMFullTextGrader(
            client, "test-model", FULL_TEXT_PROMPT_VERSION, "chat_completions", "text"
        )
        with pytest.raises(ProviderError, match="AI_UNAVAILABLE") as caught:
            adapter.grade(rubric, document)
    assert 120 < caught.value.retry_after_seconds <= 125
    assert len(requests) == 1
