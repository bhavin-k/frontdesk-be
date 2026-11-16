"""The immutable rubric and exact evidence contract for full-text scoring."""

import json
from dataclasses import asdict, dataclass
from functools import lru_cache
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import Grade, Page
from recruitment.domain.matching import EvidenceValidationError, find_quote_span, scoring_text

FULL_TEXT_PROMPT_VERSION = "matching-full-text-v1"
MAX_QUOTE_LENGTH = 600


class QuoteSelection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    source_id: str = Field(min_length=1, max_length=32)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_LENGTH)


class FullTextGrade(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    criterion_id: str
    status: Grade
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=500)
    citations: list[QuoteSelection] = Field(max_length=4)


class FullTextResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    grades: list[FullTextGrade] = Field(min_length=1, max_length=30)


class DocumentEvidence(BaseModel):
    source_id: str
    source_kind: Literal["page", "document"]
    page: int | None
    char_start: int
    char_end: int
    quote: str


@dataclass(frozen=True)
class RubricCriterion:
    criterion_id: str
    text: str
    required: bool
    strict: bool


@dataclass(frozen=True)
class FullTextRubric:
    tenant_id: UUID
    opening_id: UUID
    source_revision: int
    version_id: UUID
    criteria: tuple[RubricCriterion, ...]


@lru_cache(maxsize=256)
def rubric_prefix(rubric: FullTextRubric) -> str:
    value = json.dumps(
        [asdict(criterion) for criterion in rubric.criteria],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"\nRubric (reference data): {value}"


@dataclass(frozen=True)
class CVSource:
    id: str
    page: int | None
    original: str
    masked: str


@dataclass(frozen=True)
class FullTextDocument:
    sources: tuple[CVSource, ...]
    cv_id: UUID | None = None

    @classmethod
    def from_pages(
        cls, pages: list[Page], identities: list[str], *, cv_id: UUID | None = None
    ) -> "FullTextDocument":
        sources = tuple(
            CVSource(f"s{i}", page.number, page.text, scoring_text(page.text, identities))
            for i, page in enumerate(pages, 1)
        )
        if not any(source.masked.strip() for source in sources):
            raise ProviderError("NO_SCORABLE_TEXT")
        return cls(sources, cv_id)

    def request(self) -> str:
        return json.dumps(
            {"cv": [{"source_id": source.id, "text": source.masked} for source in self.sources]},
            ensure_ascii=False,
            separators=(",", ":"),
        )


@dataclass(frozen=True)
class FullTextResult:
    grades: list[FullTextGrade]
    evidence: dict[str, list[DocumentEvidence]]


def _citation_evidence(sources: dict[str, CVSource], citation: QuoteSelection) -> DocumentEvidence:
    source = sources.get(citation.source_id)
    if source is None:
        raise EvidenceValidationError("UNKNOWN_SOURCE_ID")
    if not citation.quote.strip():
        raise EvidenceValidationError("EMPTY_QUOTE")
    span = find_quote_span(source.masked, citation.quote)
    if span is None:
        raise EvidenceValidationError("QUOTE_NOT_FOUND_OR_REDACTED")
    start, end = span
    original = source.original[start:end]
    if original != source.masked[start:end] or len(original) > MAX_QUOTE_LENGTH:
        raise EvidenceValidationError("QUOTE_NOT_FOUND_OR_REDACTED")
    return DocumentEvidence(
        source_id=source.id,
        source_kind="page" if source.page is not None else "document",
        page=source.page,
        char_start=start,
        char_end=end,
        quote=original,
    )


def verify_full_text(
    rubric: FullTextRubric,
    document: FullTextDocument,
    response: FullTextResponse,
    *,
    allow_unverified: bool = False,
) -> FullTextResult:
    expected = [criterion.criterion_id for criterion in rubric.criteria]
    grades = {grade.criterion_id: grade for grade in response.grades}
    if len(grades) != len(response.grades) or grades.keys() != set(expected):
        raise ProviderError("INVALID_CRITERION_COVERAGE")
    sources = {source.id: source for source in document.sources}
    evidence: dict[str, list[DocumentEvidence]] = {}
    for identifier, grade in grades.items():
        positive = grade.status in {"met", "partial"}
        verified_citations = []
        evidence[identifier] = []
        for citation in grade.citations:
            try:
                evidence[identifier].append(_citation_evidence(sources, citation))
            except EvidenceValidationError:
                if not allow_unverified or not positive:
                    raise
            else:
                verified_citations.append(citation)

        if positive and not verified_citations:
            if not allow_unverified:
                raise EvidenceValidationError("EVIDENCE_REQUIRED", "EVIDENCE_REQUIRED")
            grades[identifier] = grade.model_copy(
                update={
                    "status": "unknown",
                    "confidence": 0.0,
                    "reason": "Supporting CV evidence could not be verified.",
                    "citations": [],
                }
            )
        elif len(verified_citations) != len(grade.citations):
            grades[identifier] = grade.model_copy(update={"citations": verified_citations})
    return FullTextResult([grades[key] for key in expected], evidence)
