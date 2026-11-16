from datetime import date
from decimal import Decimal

import pytest

from recruitment.adapters.experience import FakeExperience
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import Criterion, EvidenceGrade
from recruitment.domain.experience import (
    ExperienceSource,
    date_supported,
    eligibility,
    validate_facts,
)
from recruitment.domain.matching import calculate_verified_match


def assess(lines, jd="2 years relevant experience", as_of=date(2026, 11, 6)):
    sources = [ExperienceSource(id=f"cv{i}", text=line) for i, line in enumerate(lines)]
    sources.append(ExperienceSource(id="jd", text=jd, opening_id="role"))
    facts = FakeExperience().extract(sources, ["role"])
    validate_facts(facts, sources, ["role"])
    return eligibility(facts.roles[0], facts, sources, as_of, 0.99), facts, sources


def test_internships_are_excluded_before_comparing_two_year_minimum():
    result, _, _ = assess(
        [
            "Software Engineer Intern 2026-01 to 2026-07 Python",
            "Software Engineer 2026-07 to Present Python",
        ]
    )
    assert result.professional_months == 4 and result.status == "failed"
    assert result.required_relevant_months == 24
    assert "4 months" in result.reason and "24 months" in result.reason


@pytest.mark.parametrize(
    "start,expected", [("2024-12", "failed"), ("2024-11", "passed"), ("2024-10", "passed")]
)
def test_exact_minimum_boundary_uses_receipt_month(start, expected):
    result, _, _ = assess([f"Software Engineer {start} to Present Python"])
    assert result.status == expected


def test_overlapping_professional_work_counts_once():
    result, _, _ = assess(
        [
            "Software Engineer 2023-01 to 2025-01 Python",
            "Consultant 2024-01 to 2026-01 Python",
        ]
    )
    assert result.professional_months == 36 and result.status == "passed"


def test_missing_dates_are_unknown_and_no_minimum_skips_gate():
    result, _, _ = assess(["Software Engineer 2026-01 Python"])
    assert result.status == "unknown"
    result, _, _ = assess([], "Python development")
    assert result.status == "not_required"


def test_relevance_must_be_supported_separately_from_total_work():
    _, facts, sources = assess(["Engineer 2023-01 to 2026-01 Python"])
    facts.employment[0].relevant_opening_ids = []
    result = eligibility(facts.roles[0], facts, sources, date(2026, 11, 6), 0.99)
    assert result.status == "unknown"
    facts.employment[0].irrelevant_opening_ids = ["role"]
    assert eligibility(facts.roles[0], facts, sources, date(2026, 11, 6), 0.99).status == "failed"


@pytest.mark.parametrize("corruption", ["date", "source", "amount", "internship"])
def test_invalid_experience_facts_cannot_pass_source_validation(corruption):
    _, facts, sources = assess(["Software Engineer 2023-01 to Present Python"])
    if corruption == "date":
        facts.employment[0].start_month = "2010-01"
    elif corruption == "source":
        facts.employment[0].evidence_ids = ["other-cv"]
    elif corruption == "amount":
        facts.roles[0].requirements[0].amount = Decimal(20)
    else:
        facts.employment[0].is_internship = True
    with pytest.raises(ProviderError, match="INVALID_EXPERIENCE_EVIDENCE"):
        validate_facts(facts, sources, ["role"])


def test_unknown_criterion_shows_provisional_supported_points():
    criteria = [Criterion(text=f"Skill {i}") for i in range(9)]
    grades = [
        EvidenceGrade(
            status="met",
            confidence=0.99,
            evidence_chunk_ids=["fixture"],
            quotes=["fixture"],
            reason="Fixture evidence.",
        )
        for _ in range(8)
    ]
    grades.append(
        EvidenceGrade(
            status="unknown", confidence=0.5, evidence_chunk_ids=[], quotes=[], reason="Unverified."
        )
    )
    result = calculate_verified_match(criteria, grades, Decimal(75))
    assert result.score == Decimal("88.89") and result.provisional
    assert result.recommendation == "needs_review"


@pytest.mark.parametrize(
    "text", ["Nov2026", "November 2026", "Nov. 2026", "2026-11", "2026/11", "11/2026"]
)
def test_cv_month_formats_are_verified_without_rewriting_source(text):
    assert date_supported("2026-11", text)
    assert not date_supported("2025-11", text)


def test_domain_minimum_cannot_be_satisfied_by_unrelated_professional_months():
    _, facts, sources = assess(["Engineer 2023-01 to 2026-01 Python; no NLP work"])
    facts.roles[0].requirements[0].domain = "NLP"
    facts.employment[0].relevant_domains = ["Python"]
    facts.employment[0].irrelevant_domains = ["NLP"]
    result = eligibility(facts.roles[0], facts, sources, date(2026, 11, 6), 0.99)
    assert result.professional_months == 36 and result.status == "failed"
    assert result.checks[0]["counted_months"] == 0
    facts.employment[0].irrelevant_domains = []
    assert eligibility(facts.roles[0], facts, sources, date(2026, 11, 6), 0.99).status == "unknown"
