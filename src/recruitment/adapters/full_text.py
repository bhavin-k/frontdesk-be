"""Structured full-text grading with one repair after an invalid response."""

import logging
import time
from functools import partial

from openai import OpenAI

from recruitment.adapters.ai import StructuredAI
from recruitment.adapters.ai_requests import TokenUsage, request_with_rate_limit_retries
from recruitment.core.config import (
    DEFAULT_FULL_TEXT_OUTPUT_TOKENS,
    DEFAULT_FULL_TEXT_REQUEST_TOKENS,
    LLMAPIFormat,
    LLMOutputMode,
)
from recruitment.core.errors import ProviderError
from recruitment.domain.full_text import (
    FullTextDocument,
    FullTextResponse,
    FullTextResult,
    FullTextRubric,
    rubric_prefix,
    verify_full_text,
)

REPAIRABLE_ERRORS = {
    "INVALID_MODEL_OUTPUT",
    "INVALID_CRITERION_COVERAGE",
    "INVALID_EVIDENCE",
    "EVIDENCE_REQUIRED",
}
logger = logging.getLogger(__name__)


class LLMFullTextGrader(StructuredAI):
    def __init__(
        self,
        client: OpenAI,
        model: str,
        prompt: str,
        api_format: LLMAPIFormat = "responses",
        output_mode: LLMOutputMode = "json_schema",
        *,
        request_tokens: int = DEFAULT_FULL_TEXT_REQUEST_TOKENS,
        output_tokens: int = DEFAULT_FULL_TEXT_OUTPUT_TOKENS,
    ) -> None:
        super().__init__(client, model, prompt, api_format, output_mode)
        self.request_tokens, self.output_tokens = request_tokens, output_tokens

    def grade(self, rubric: FullTextRubric, document: FullTextDocument) -> FullTextResult:
        prefix = rubric_prefix(rubric)
        value = document.request()
        usage: list[TokenUsage] = []
        attempts = 0
        started = time.monotonic()
        outcome = "INTERNAL_ERROR"

        def request(instructions: str) -> FullTextResponse:
            nonlocal attempts
            attempts += 1
            return self.parse(
                value,
                FullTextResponse,
                max_attempts=1,
                extra_instructions=instructions,
                output_tokens=self.output_tokens,
                observe_usage=usage.append,
            )

        correction = ""
        try:
            for attempt in range(2):
                instructions = prefix + correction
                input_tokens = self.estimate_input_tokens(value, FullTextResponse, instructions)
                if input_tokens + self.output_tokens > self.request_tokens:
                    raise ProviderError("FULL_TEXT_REQUEST_BUDGET_EXCEEDED")
                try:
                    response = request_with_rate_limit_retries(partial(request, instructions))
                    result = verify_full_text(
                        rubric, document, response, allow_unverified=attempt == 1
                    )
                    outcome = "success"
                    return result
                except ProviderError as exc:
                    if attempt or exc.code not in REPAIRABLE_ERRORS:
                        raise
                    correction = (
                        f"\nRepair the previous response ({exc.code}). "
                        "Use one grade per criterion and exact quotations from the source."
                    )
            raise ProviderError("INVALID_MODEL_OUTPUT")
        except ProviderError as exc:
            outcome = exc.code
            raise
        finally:
            cached = [item.cached_tokens for item in usage if item.cached_tokens is not None]
            logger.info(
                "full_text.grading cv_id=%s opening_id=%s rubric_version_id=%s model=%s "
                "input_tokens=%s output_tokens=%s cached_tokens=%s latency_ms=%s attempts=%s "
                "outcome=%s",
                document.cv_id,
                rubric.opening_id,
                rubric.version_id,
                self.model,
                sum(item.input_tokens for item in usage) if usage else None,
                sum(item.output_tokens for item in usage) if usage else None,
                sum(cached) if cached else None,
                round((time.monotonic() - started) * 1000),
                attempts,
                outcome,
            )
