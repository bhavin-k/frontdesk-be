"""Job setup contracts and backend-owned matching policy."""

import re
from datetime import UTC
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import Criterion, StrictModel
from recruitment.domain.matching import validate_criteria

OpeningStatus = Literal["active", "paused", "closed"]
PreparationStatus = Literal["queued", "processing", "ready", "failed"]
SHORTLIST_THRESHOLD = 75


class JobDetails(StrictModel):
    department: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    total_experience_years: float | None = Field(default=None, ge=0, le=80)
    relevant_experience_years: float | None = Field(default=None, ge=0, le=80)
    domain_experience: str | None = Field(default=None, max_length=4000)
    candidate_requirements: str | None = Field(default=None, max_length=10000)
    employment_type: str | None = Field(default=None, max_length=100)
    salary_min: int | None = Field(default=None, ge=0)
    salary_max: int | None = Field(default=None, ge=0)
    headcount: int | None = Field(default=None, ge=1, le=100000)
    application_deadline: AwareDatetime | None = None

    @field_validator("application_deadline")
    @classmethod
    def utc_deadline(cls, value: AwareDatetime | None) -> AwareDatetime | None:
        return value.astimezone(UTC) if value else None

    @model_validator(mode="after")
    def ranges(self) -> "JobDetails":
        if (
            self.salary_min is not None
            and self.salary_max is not None
            and self.salary_min > self.salary_max
        ):
            raise ValueError("Salary minimum cannot exceed the maximum.")
        if (
            self.total_experience_years is not None
            and self.relevant_experience_years is not None
            and self.relevant_experience_years > self.total_experience_years
        ):
            raise ValueError("Relevant experience cannot exceed total experience.")
        return self


class OpeningCreate(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    job_description: str = Field(min_length=10, max_length=20000)
    details: JobDetails = Field(default_factory=JobDetails)
    questions: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("title", "job_description", mode="before")
    @classmethod
    def meaningful_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("questions")
    @classmethod
    def question_text(cls, value: list[str]) -> list[str]:
        if any(not text.strip() or len(text) > 2000 for text in value):
            raise ValueError("Each question must contain 1 to 2000 characters.")
        return [text.strip() for text in value]


class OpeningUpdate(OpeningCreate):
    expected_revision: int = Field(ge=1)


class OpeningAction(StrictModel):
    action: Literal["pause", "continue", "close"]
    expected_revision: int = Field(ge=1)


class RequirementSource(StrictModel):
    id: str
    text: str


class ExtractedRequirement(StrictModel):
    text: str = Field(min_length=3, max_length=500)
    required: bool
    strict: bool = False
    source_id: str
    quote: str = Field(min_length=1, max_length=2000)


class GeneratedRequirements(StrictModel):
    criteria: list[ExtractedRequirement] = Field(min_length=1, max_length=30)


def checked_requirements(
    result: GeneratedRequirements, sources: list[RequirementSource]
) -> list[tuple[Criterion, dict[str, str]]]:
    supplied = {source.id: source.text for source in sources}
    checked = []
    for item in result.criteria:
        if (
            item.source_id not in supplied
            or not item.quote.strip()
            or item.quote not in supplied[item.source_id]
        ):
            raise ProviderError("INVALID_JOB_EVIDENCE")
        strict = item.strict and bool(
            re.search(r"\b(must|mandatory|non-negotiable)\b", item.quote, re.I)
        )
        criterion = Criterion(text=item.text, required=item.required, strict=strict, weight=1)
        validate_criteria([criterion])
        checked.append((criterion, {"source_id": item.source_id, "quote": item.quote}))
    return checked
