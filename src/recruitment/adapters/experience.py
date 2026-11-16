"""Real and offline experience fact providers, behind the same contract."""

import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from openai import OpenAI

from recruitment.adapters.ai import StructuredAI
from recruitment.adapters.ai_requests import request_with_rate_limit_retries
from recruitment.core.config import (
    DEFAULT_EXPERIENCE_OUTPUT_TOKENS,
    DEFAULT_EXPERIENCE_REQUEST_TOKENS,
    LLMAPIFormat,
    LLMOutputMode,
)
from recruitment.core.errors import ProviderError
from recruitment.domain.experience import (
    EmploymentEvidence,
    ExperienceFacts,
    ExperienceMinimum,
    ExperienceSource,
    RoleExperience,
)

EXPERIENCE_PROMPT_VERSION = "experience-v2"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CompactExperienceRequest:
    value: str
    sources: dict[str, str]
    openings: dict[str, str]

    @classmethod
    def build(
        cls, sources: list[ExperienceSource], opening_ids: list[str]
    ) -> "CompactExperienceRequest":
        openings = {f"r{index}": key for index, key in enumerate(opening_ids, 1)}
        groups: dict[str | None, list[tuple[str, str]]] = {key: [] for key in [None, *opening_ids]}
        source_ids = {}
        for index, source in enumerate(sources, 1):
            key = f"s{index}"
            source_ids[key] = source.id
            groups[source.opening_id].append((key, source.text))
        payload = {
            "cv": groups[None],
            "openings": [{"id": key, "sources": groups[value]} for key, value in openings.items()],
        }
        return cls(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")), source_ids, openings
        )

    def resolve(self, facts: ExperienceFacts) -> ExperienceFacts:
        try:
            return ExperienceFacts(
                roles=[
                    role.model_copy(
                        update={
                            "opening_id": self.openings[role.opening_id],
                            "requirements": [
                                item.model_copy(
                                    update={
                                        "evidence_ids": [self.sources[k] for k in item.evidence_ids]
                                    }
                                )
                                for item in role.requirements
                            ],
                        }
                    )
                    for role in facts.roles
                ],
                employment=[
                    job.model_copy(
                        update={
                            "relevant_opening_ids": [
                                self.openings[k] for k in job.relevant_opening_ids
                            ],
                            "irrelevant_opening_ids": [
                                self.openings[k] for k in job.irrelevant_opening_ids
                            ],
                            "evidence_ids": [self.sources[k] for k in job.evidence_ids],
                        }
                    )
                    for job in facts.employment
                ],
                complete_employment=facts.complete_employment,
            )
        except KeyError:
            raise ProviderError("INVALID_EXPERIENCE_EVIDENCE") from None


class LLMExperience(StructuredAI):
    inline_chat_schema = False

    def __init__(
        self,
        client: OpenAI,
        model: str,
        prompt: str,
        api_format: LLMAPIFormat = "responses",
        output_mode: LLMOutputMode = "json_schema",
        *,
        request_tokens: int = DEFAULT_EXPERIENCE_REQUEST_TOKENS,
        output_tokens: int = DEFAULT_EXPERIENCE_OUTPUT_TOKENS,
    ) -> None:
        super().__init__(client, model, prompt, api_format, output_mode)
        self.request_tokens, self.output_tokens = request_tokens, output_tokens

    def extract(self, sources: list[ExperienceSource], opening_ids: list[str]) -> ExperienceFacts:
        request = CompactExperienceRequest.build(sources, opening_ids)
        input_tokens = self.estimate_input_tokens(request.value, ExperienceFacts, "")
        if input_tokens + self.output_tokens > self.request_tokens:
            logger.info(
                "experience.request_budget_exceeded input_tokens=%s output_reserve=%s budget=%s",
                input_tokens,
                self.output_tokens,
                self.request_tokens,
            )
            raise ProviderError("EXPERIENCE_REQUEST_BUDGET_EXCEEDED")
        for attempt in range(2):
            try:
                facts = request_with_rate_limit_retries(
                    lambda: self.parse(
                        request.value,
                        ExperienceFacts,
                        max_attempts=1,
                        output_tokens=self.output_tokens,
                    )
                )
                return request.resolve(facts)
            except ProviderError as error:
                if attempt or error.code not in {
                    "INVALID_MODEL_OUTPUT",
                    "MODEL_OUTPUT_UNAVAILABLE",
                }:
                    raise
        raise ProviderError("INVALID_MODEL_OUTPUT")


class FakeExperience:
    """Only reads explicit fixture dates and minimums; does not simulate provider access."""

    model = "fake-experience-v1"

    def extract(self, sources: list[ExperienceSource], opening_ids: list[str]) -> ExperienceFacts:
        roles = []
        for opening_id in opening_ids:
            requirements = []
            for source in sources:
                if source.opening_id != opening_id or re.search(
                    r"preferred|optional", source.text, re.I
                ):
                    continue
                field = re.search(
                    r"(total|relevant) experience years:\s*(\d+(?:\.\d+)?)", source.text, re.I
                )
                minimum = re.search(
                    r"(\d+(?:\.\d+)?)\s*\+?\s*(years?|months?|yrs?)", source.text, re.I
                )
                if not field and not minimum:
                    continue
                kind: Literal["total", "relevant"] = (
                    "total" if field and field[1].lower() == "total" else "relevant"
                )
                amount = field[2] if field else minimum[1]  # type: ignore[index]
                unit: Literal["years", "months"] = (
                    "months" if minimum and minimum[2].lower().startswith("month") else "years"
                )
                requirements.append(
                    ExperienceMinimum(
                        kind=kind,
                        amount=Decimal(amount),
                        unit=unit,
                        confidence=0.99,
                        evidence_ids=[source.id],
                    )
                )
            roles.append(RoleExperience(opening_id=opening_id, requirements=requirements))
        jobs = []
        for source in sources:
            if source.opening_id is not None:
                continue
            dates = re.findall(r"\b\d{4}-(?:0[1-9]|1[0-2])\b", source.text)
            if not dates:
                continue
            current = bool(re.search(r"\b(present|current)\b", source.text, re.I))
            jobs.append(
                EmploymentEvidence(
                    start_month=dates[0],
                    end_month=dates[1] if len(dates) > 1 else None,
                    current=current,
                    is_internship=bool(re.search(r"\bintern(ship)?\b", source.text, re.I)),
                    relevant_opening_ids=opening_ids,
                    irrelevant_opening_ids=[],
                    confidence=0.99,
                    evidence_ids=[source.id],
                )
            )
        return ExperienceFacts(roles=roles, employment=jobs, complete_employment=bool(jobs))
