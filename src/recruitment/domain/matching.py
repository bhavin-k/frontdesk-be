import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from recruitment.core.errors import AppError, ProviderError
from recruitment.domain.contracts import (
    Criterion,
    EvidenceGrade,
    EvidenceInput,
    Grade,
    Recommendation,
)

POINTS = {"met": Decimal(1), "partial": Decimal("0.5"), "missing": Decimal(0)}
PROTECTED = re.compile(
    r"\b(age|date of birth|gender|sex|race|caste|religion|marital|pregnan\w*|disabil\w*|"
    r"nationality|ethnicity|sexual orientation|male|female|young|old|hindu|muslim|christian)\b",
    re.IGNORECASE,
)


def validate_criteria(criteria: list[Criterion]) -> None:
    if any(PROTECTED.search(item.text) for item in criteria):
        raise AppError("DISALLOWED_CRITERION", "Use job-related requirements only.")
    if any(item.strict and not item.required for item in criteria):
        raise AppError("INVALID_CRITERION", "A strict condition must be required.")


def scoring_text(text: str, identity_values: list[str]) -> str:
    """Preserve source length so redacted spans retain the original offsets."""
    for value in sorted(filter(None, identity_values), key=len, reverse=True):
        text = re.sub(re.escape(value), lambda match: " " * len(match[0]), text, flags=re.I)
    text = re.sub(
        r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\+?\d[\d ()-]{8,}\d",
        lambda match: " " * len(match[0]),
        text,
    )
    return "\n".join(
        " " * len(line) if PROTECTED.search(line) else line for line in text.split("\n")
    )


class EvidenceValidationError(ProviderError):
    def __init__(self, detail: str, code: str = "INVALID_EVIDENCE") -> None:
        super().__init__(code)
        self.detail = detail


def validate_evidence(grade: EvidenceGrade, chunks: list[EvidenceInput]) -> None:
    supplied = {chunk.chunk_id: chunk.text for chunk in chunks}
    if len(grade.evidence_chunk_ids) != len(grade.quotes):
        raise EvidenceValidationError("CITATION_COUNT_MISMATCH")
    for chunk_id, quote in zip(grade.evidence_chunk_ids, grade.quotes, strict=True):
        if chunk_id not in supplied:
            raise EvidenceValidationError("UNKNOWN_CHUNK_ID")
        if not quote.strip():
            raise EvidenceValidationError("EMPTY_QUOTE")
        if quote not in supplied[chunk_id]:
            raise EvidenceValidationError("QUOTE_NOT_FOUND")
    if grade.status in {"met", "partial", "missing"} and not grade.quotes:
        raise EvidenceValidationError("EVIDENCE_REQUIRED", "EVIDENCE_REQUIRED")


def find_quote_span(source: str, quote: str) -> tuple[int, int] | None:
    """Return source offsets, allowing whitespace-only differences."""
    start = source.find(quote)
    if start >= 0:
        return start, start + len(quote)

    spans = list(re.finditer(r"\s+|\S", source))
    normalized = "".join(" " if span[0].isspace() else span[0] for span in spans)
    needle = re.sub(r"\s+", " ", quote).strip()
    offset = normalized.find(needle)
    if not needle or offset < 0:
        return None
    return spans[offset].start(), spans[offset + len(needle) - 1].end()


def canonicalize_evidence(grade: EvidenceGrade, chunks: list[EvidenceInput]) -> EvidenceGrade:
    """Resolve whitespace-only quote differences to an exact supplied source span."""
    supplied = {chunk.chunk_id: chunk.text for chunk in chunks}
    if len(grade.evidence_chunk_ids) != len(grade.quotes):
        raise EvidenceValidationError("CITATION_COUNT_MISMATCH")
    quotes = []
    for chunk_id, quote in zip(grade.evidence_chunk_ids, grade.quotes, strict=True):
        if chunk_id not in supplied:
            raise EvidenceValidationError("UNKNOWN_CHUNK_ID")
        if not quote.strip():
            raise EvidenceValidationError("EMPTY_QUOTE")
        source = supplied[chunk_id]
        span = find_quote_span(source, quote)
        if span is None:
            raise EvidenceValidationError("QUOTE_NOT_FOUND")
        start, end = span
        quotes.append(source[start:end])
    resolved = grade.model_copy(update={"quotes": quotes})
    validate_evidence(resolved, chunks)
    return resolved


@dataclass(frozen=True)
class MatchResult:
    score: Decimal | None
    recommendation: Recommendation
    reason: str
    missing_required: list[int]
    provisional: bool = False


class CriterionGrade(Protocol):
    @property
    def status(self) -> Grade: ...

    @property
    def confidence(self) -> float: ...


def calculate_match(
    criteria: list[Criterion], grades: Sequence[CriterionGrade], threshold: Decimal
) -> MatchResult:
    if not criteria or len(criteria) != len(grades):
        raise ProviderError("INCOMPLETE_GRADING")
    missing = [
        i
        for i, (c, g) in enumerate(zip(criteria, grades, strict=True))
        if c.required and g.status in {"missing", "partial"}
    ]
    if any(g.status == "unknown" or g.confidence < 0.8 for g in grades):
        return MatchResult(None, "needs_review", "Evidence requires recruiter review.", missing)
    total = sum((Decimal(str(c.weight)) for c in criteria), Decimal(0))
    earned = sum(
        (Decimal(str(c.weight)) * POINTS[g.status] for c, g in zip(criteria, grades, strict=True)),
        Decimal(0),
    )
    score = (earned / total * 100).quantize(Decimal("0.01"))
    strict_failure = any(
        c.strict and g.status == "missing" for c, g in zip(criteria, grades, strict=True)
    )
    if strict_failure:
        return MatchResult(
            score, "recommended_rejection", "A strict requirement is not met.", missing
        )
    if any(c.required and g.status == "partial" for c, g in zip(criteria, grades, strict=True)):
        return MatchResult(
            score, "needs_review", "A required criterion has incomplete evidence.", missing
        )
    if abs(score - threshold) <= 5:
        return MatchResult(score, "needs_review", "The score is close to the threshold.", missing)
    if score >= threshold and not missing:
        return MatchResult(
            score, "recommended_shortlist", "CV evidence meets the approved requirements.", missing
        )
    return MatchResult(
        score,
        "recommended_rejection",
        "CV evidence does not meet the approved requirements.",
        missing,
    )


def calculate_verified_match(
    criteria: list[Criterion], grades: Sequence[CriterionGrade], threshold: Decimal
) -> MatchResult:
    """Show supported points while preserving uncertainty and human review."""
    if not criteria or len(criteria) != len(grades):
        raise ProviderError("INCOMPLETE_GRADING")
    uncertain = any(g.status == "unknown" or g.confidence < 0.8 for g in grades)
    missing = [
        i
        for i, (c, g) in enumerate(zip(criteria, grades, strict=True))
        if c.required and (g.status != "met" or g.confidence < 0.8)
    ]
    total = sum((Decimal(str(c.weight)) for c in criteria), Decimal(0))
    points = sum(
        (
            Decimal(str(c.weight))
            * (POINTS.get(g.status, Decimal(0)) if g.confidence >= 0.8 else Decimal(0))
            for c, g in zip(criteria, grades, strict=True)
        ),
        Decimal(0),
    )
    score = (points / total * 100).quantize(Decimal("0.01"))
    if uncertain:
        return MatchResult(
            score,
            "needs_review",
            "Some criteria remain unverified; the score is provisional.",
            missing,
            True,
        )
    return calculate_match(criteria, grades, threshold)
