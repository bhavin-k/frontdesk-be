import json
import logging
from collections.abc import Callable
from importlib.resources import files
from typing import Any, Literal, TypeVar

from openai import (
    APIConnectionError,
    APIResponseValidationError,
    APIStatusError,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    OpenAI,
    RateLimitError,
    omit,
)
from openai.types import CompletionUsage
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from recruitment.adapters.ai_requests import TokenUsage, request_with_rate_limit_retries
from recruitment.core.config import LLMAPIFormat, LLMOutputMode
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import CandidateProfile, JobDescription, MatchResult
from recruitment.domain.tokenization import token_count

T = TypeVar("T", bound=BaseModel)
logger = logging.getLogger(__name__)
SAFE_PROVIDER_ERROR_CODES = frozenset(
    {
        "context_length_exceeded",
        "invalid_request",
        "invalid_request_error",
        "json_validate_failed",
        "rate_limit_error",
        "rate_limit_exceeded",
        "request_too_large",
        "request_too_large_for_model",
    }
)


def _log_request_failure(error: APIStatusError) -> None:
    code = error.code
    if not isinstance(code, str) or code not in SAFE_PROVIDER_ERROR_CODES:
        code = "unclassified"
    logger.warning("ai.request_failed status_code=%s provider_code=%s", error.status_code, code)


class StructuredAI:
    inline_chat_schema = True
    chat_temperature: float | None = None

    def __init__(
        self,
        client: OpenAI,
        model: str,
        prompt: str,
        api_format: LLMAPIFormat = "responses",
        output_mode: LLMOutputMode = "json_schema",
        *,
        request_tokens: int = 32_768,
        output_tokens: int = 3072,
        chat_disable_thinking: bool = False,
    ) -> None:
        self.client, self.model, self.prompt = client, model, prompt
        self.api_format, self.output_mode = api_format, output_mode
        self.request_tokens, self.output_tokens = request_tokens, output_tokens
        self.chat_extra_body = {"thinking": {"type": "disabled"}} if chat_disable_thinking else None

    def prompt_text(self, extra_instructions: str) -> str:
        return (
            files("recruitment").joinpath(f"prompts/{self.prompt}.txt").read_text("utf-8")
            + extra_instructions
        )

    def _chat_instructions(self, prompt: str, schema: type[BaseModel]) -> str:
        if self.output_mode == "json_schema" and not self.inline_chat_schema:
            return f"{prompt}\nReturn one JSON object matching the response schema."
        schema_json = json.dumps(
            schema.model_json_schema(), ensure_ascii=False, separators=(",", ":")
        )
        return (
            f"{prompt}\nReturn one JSON object matching this schema. No Markdown or extra text.\n"
            f"{schema_json}"
        )

    def estimate_input_tokens(
        self, value: str, schema: type[BaseModel], extra_instructions: str
    ) -> int:
        prompt = self.prompt_text(extra_instructions)
        if self.api_format == "chat_completions":
            prompt = self._chat_instructions(prompt, schema)
        tokens = token_count(prompt) + token_count(value)
        if self.output_mode == "json_schema":
            tokens += token_count(json.dumps(schema.model_json_schema()))
        # Allow for tokenizer differences and message framing.
        return (tokens * 110 + 99) // 100 + 64

    def parse(
        self,
        value: str,
        schema: type[T],
        *,
        max_attempts: Literal[1, 2] = 2,
        extra_instructions: str = "",
        output_tokens: int | None = None,
        observe_usage: Callable[[TokenUsage], None] | None = None,
    ) -> T:
        prompt = self.prompt_text(extra_instructions)
        for attempt in range(max_attempts):
            try:
                if self.api_format == "responses":
                    response = self.client.responses.parse(
                        model=self.model,
                        store=False,
                        instructions=prompt,
                        input=value,
                        text_format=schema,
                        max_output_tokens=output_tokens if output_tokens is not None else omit,
                    )
                    if observe_usage is not None and response.usage is not None:
                        details = response.usage.input_tokens_details
                        observe_usage(
                            TokenUsage(
                                response.usage.input_tokens,
                                response.usage.output_tokens,
                                details.cached_tokens if details is not None else None,
                            )
                        )
                    if response.output_parsed is None:
                        raise ProviderError("MODEL_OUTPUT_UNAVAILABLE")
                    return schema.model_validate(response.output_parsed)
                return self._chat(prompt, value, schema, output_tokens, observe_usage)
            except ValidationError:
                if attempt + 1 == max_attempts:
                    raise ProviderError("INVALID_MODEL_OUTPUT") from None
                prompt += (
                    "\nThe previous response failed schema validation. Return the exact schema."
                )
            except RateLimitError as exc:
                _log_request_failure(exc)
                raise ProviderError("AI_UNAVAILABLE", retryable=True) from exc
            except APIConnectionError as exc:
                raise ProviderError("AI_UNAVAILABLE", retryable=True) from exc
            except (
                APIResponseValidationError,
                ContentFilterFinishReasonError,
                LengthFinishReasonError,
                AttributeError,
                TypeError,
                ValueError,
            ):
                raise ProviderError("MODEL_OUTPUT_UNAVAILABLE") from None
            except APIStatusError as exc:
                _log_request_failure(exc)
                if exc.status_code == 400 and exc.code == "json_validate_failed":
                    if attempt + 1 == max_attempts:
                        raise ProviderError("INVALID_MODEL_OUTPUT") from None
                    prompt += "\nGeneration failed JSON validation. Return the exact schema."
                    continue
                raise ProviderError(
                    "AI_REQUEST_FAILED", retryable=exc.status_code >= 500 or exc.status_code == 408
                ) from exc
        raise ProviderError("INVALID_MODEL_OUTPUT")

    def _chat(
        self,
        prompt: str,
        value: str,
        schema: type[T],
        output_tokens: int | None,
        observe_usage: Callable[[TokenUsage], None] | None,
    ) -> T:
        instructions = self._chat_instructions(prompt, schema)
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": value},
        ]
        temperature = self.chat_temperature if self.chat_temperature is not None else omit
        if self.output_mode == "json_schema":
            completion = self.client.chat.completions.parse(
                model=self.model,
                messages=messages,
                store=False,
                temperature=temperature,
                response_format=schema,
                max_completion_tokens=output_tokens if output_tokens is not None else omit,
                extra_body=self.chat_extra_body,
            )
            self._observe_chat_usage(completion.usage, observe_usage)
            if (
                not completion.choices
                or completion.choices[0].finish_reason != "stop"
                or completion.choices[0].message.refusal
                or completion.choices[0].message.parsed is None
            ):
                raise ProviderError("MODEL_OUTPUT_UNAVAILABLE")
            return schema.model_validate(completion.choices[0].message.parsed)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            store=False,
            temperature=temperature,
            response_format={"type": "json_object"} if self.output_mode == "json_object" else omit,
            max_completion_tokens=output_tokens if output_tokens is not None else omit,
            extra_body=self.chat_extra_body,
        )
        self._observe_chat_usage(response.usage, observe_usage)
        reason = "no_choices"
        if response.choices:
            choice = response.choices[0]
            if choice.message.refusal:
                reason = "refusal"
            elif choice.finish_reason == "stop":
                if choice.message.content:
                    return schema.model_validate_json(choice.message.content)
                reason = "empty_content"
            elif choice.finish_reason in {
                "length",
                "content_filter",
                "tool_calls",
                "function_call",
            }:
                reason = choice.finish_reason
            else:
                reason = "unknown_finish_reason"
        logger.warning(
            "ai.output_unavailable prompt=%s reason=%s output_tokens=%s limit=%s",
            self.prompt,
            reason,
            response.usage.completion_tokens if response.usage is not None else None,
            output_tokens,
        )
        raise ProviderError("MODEL_OUTPUT_UNAVAILABLE")

    @staticmethod
    def _observe_chat_usage(
        usage: CompletionUsage | None, observe_usage: Callable[[TokenUsage], None] | None
    ) -> None:
        if usage is not None and observe_usage is not None:
            details = usage.prompt_tokens_details
            observe_usage(
                TokenUsage(
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    details.cached_tokens if details is not None else None,
                )
            )

    def bounded_parse(self, value: str, schema: type[T]) -> T:
        if self.estimate_input_tokens(value, schema, "") + self.output_tokens > self.request_tokens:
            raise ProviderError("LLM_INPUT_TOO_LARGE")
        return request_with_rate_limit_retries(
            lambda: self.parse(
                value, schema, output_tokens=self.output_tokens, observe_usage=self._log_usage
            )
        )

    def _log_usage(self, usage: TokenUsage) -> None:
        logger.info(
            "ai.token_usage prompt=%s input_tokens=%s output_tokens=%s total_tokens=%s",
            self.prompt,
            usage.input_tokens,
            usage.output_tokens,
            usage.input_tokens + usage.output_tokens,
        )


class CVParser(StructuredAI):
    def profile(self, text: str) -> CandidateProfile:
        return self.bounded_parse(json.dumps({"cv_reference_text": text}), CandidateProfile)


class JDMatcher(StructuredAI):
    def match(self, job: JobDescription, profile: dict[str, Any]) -> MatchResult:
        cv = {
            key: value
            for key, value in profile.items()
            if key not in {"name", "email", "phone"} and value not in ("", [], None)
        }
        value = json.dumps(
            {"job": job.model_dump(exclude_none=True), "cv": cv},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self.bounded_parse(value, MatchResult)
