from decimal import Decimal

import pytest

from recruitment.core.errors import AppError, ProviderError
from recruitment.domain.contracts import Criterion, EvidenceGrade, EvidenceInput
from recruitment.domain.matching import (
    calculate_match,
    canonicalize_evidence,
    scoring_text,
    validate_criteria,
    validate_evidence,
)


def grade(status: str, confidence: float = 0.99) -> EvidenceGrade:
    return EvidenceGrade(
        status=status,
        confidence=confidence,
        evidence_chunk_ids=["chunk-1"],
        quotes=["Python experience"],
        reason="Evidence from the CV.",
    )


def test_weighted_arithmetic_and_required_criteria() -> None:
    result = calculate_match(
        [Criterion(text="Python", weight=3), Criterion(text="SQL", required=False, weight=1)],
        [grade("met"), grade("partial")],
        Decimal(75),
    )
    assert result.score == Decimal("87.50")
    assert result.recommendation == "recommended_shortlist"


@pytest.mark.parametrize("threshold", [70, 75, 80])
def test_near_threshold_goes_to_review(threshold: int) -> None:
    result = calculate_match(
        [Criterion(text="Python", weight=3), Criterion(text="SQL", required=False, weight=1)],
        [grade("met"), grade("missing")],
        Decimal(threshold),
    )
    assert result.score == Decimal(75)
    assert result.recommendation == "needs_review"


@pytest.mark.parametrize("status,confidence", [("unknown", 0.99), ("met", 0.2)])
def test_uncertain_evidence_is_not_rejection(status: str, confidence: float) -> None:
    result = calculate_match([Criterion(text="Python")], [grade(status, confidence)], Decimal(75))
    assert result.recommendation == "needs_review"
    assert result.score is None


def test_explicit_strict_failure_is_only_a_recommendation() -> None:
    result = calculate_match(
        [Criterion(text="Required certification", strict=True)], [grade("missing")], Decimal(75)
    )
    assert result.recommendation == "recommended_rejection"
    assert result.missing_required == [0]


@pytest.mark.parametrize(
    "criterion",
    ["Age under 30", "Male applicants", "Religion Hindu", "No disability", "Nationality Indian"],
)
def test_protected_criteria_are_blocked(criterion: str) -> None:
    with pytest.raises(AppError, match="job-related"):
        validate_criteria([Criterion(text=criterion)])


def test_redaction_preserves_evidence_positions() -> None:
    original = "Example Person\nperson@example.org\nAge: 34\nPython development"
    safe = scoring_text(original, ["Example Person"])
    assert len(safe) == len(original)
    assert "Person" not in safe and "person@example.org" not in safe and "34" not in safe
    assert safe.index("Python development") == original.index("Python development")


def test_model_cannot_cite_another_chunk_or_invent_a_quote() -> None:
    chunks = [EvidenceInput(chunk_id="chunk-1", text="Python experience")]
    for ids, quotes in [(["other"], ["Python experience"]), (["chunk-1"], ["invented quote"])]:
        with pytest.raises(ProviderError):
            validate_evidence(
                EvidenceGrade(
                    status="met",
                    confidence=0.99,
                    evidence_chunk_ids=ids,
                    quotes=quotes,
                    reason="Unsupported model claim.",
                ),
                chunks,
            )


def test_missing_without_evidence_is_not_a_proven_failure() -> None:
    with pytest.raises(ProviderError, match="EVIDENCE_REQUIRED"):
        validate_evidence(
            EvidenceGrade(
                status="missing",
                confidence=0.99,
                evidence_chunk_ids=[],
                quotes=[],
                reason="Not mentioned.",
            ),
            [],
        )


@pytest.mark.parametrize(
    "source,quote",
    [
        ("Python development\nfor three years.", "Python development for three years."),
        ("C++\tand\u00a0C# experience", "C++ and C# experience"),
        ("  Python\r\n APIs  ", "Python APIs"),
    ],
)
def test_quote_whitespace_is_mapped_back_to_exact_source(source, quote):
    chunks = [EvidenceInput(chunk_id="chunk-1", text=source)]
    original = grade("met").model_copy(update={"quotes": [quote]})
    resolved = canonicalize_evidence(original, chunks)
    validate_evidence(resolved, chunks)
    assert resolved.quotes[0] in source
    assert resolved.evidence_chunk_ids == original.evidence_chunk_ids
    assert resolved.confidence == original.confidence and resolved.status == original.status
    assert original.quotes == [quote]


@pytest.mark.parametrize(
    "source,quote",
    [
        ("Python experience for 3 years", "Python experience for 5 years"),
        ("Python, SQL", "Python and SQL"),
        ("Python experience", "python experience"),
        ("C# experience", "C++ experience"),
    ],
)
def test_quote_resolution_does_not_accept_changed_facts_or_wording(source, quote):
    with pytest.raises(ProviderError, match="INVALID_EVIDENCE"):
        canonicalize_evidence(
            grade("met").model_copy(update={"quotes": [quote]}),
            [EvidenceInput(chunk_id="chunk-1", text=source)],
        )
