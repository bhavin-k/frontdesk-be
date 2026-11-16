from datetime import date

import pytest

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import CandidateProfile, Employment
from recruitment.domain.profiles import normalize_profile


def profile(employment=None) -> CandidateProfile:
    return CandidateProfile(
        name="  Example   Candidate ",
        email="Person@EXAMPLE.ORG",
        mobile="+91 98765 43210",
        language="en",
        skills=["python", "Python", "postgres"],
        employment=employment or [],
        education=[],
        certifications=[],
        projects=[],
        confidence=0.99,
        location=" Bengaluru   India ",
    )


def job(start: str, end: str | None, current: bool = False) -> Employment:
    return Employment(
        company="Example",
        title="Developer",
        start_month=start,
        end_month=end,
        current=current,
        description="Backend development.",
        duration_months=999,
    )


def test_normalizes_contact_location_and_skill_aliases() -> None:
    result = normalize_profile(profile())
    assert result.name == "Example Candidate"
    assert result.email == "person@example.org"
    assert result.mobile == "+919876543210"
    assert result.location == "Bengaluru India"
    assert result.skills == ["PostgreSQL", "Python"]
    assert result.experience_months is None


def test_experience_excludes_overlap_and_ignores_model_arithmetic() -> None:
    result = normalize_profile(
        profile(
            [
                job("2020-01", "2022-01"),
                job("2021-01", "2023-01"),
            ]
        ),
        as_of=date(2026, 11, 3),
    )
    assert result.experience_months == 36
    assert [entry.duration_months for entry in result.employment] == [24, 24]


def test_current_job_uses_processing_month() -> None:
    result = normalize_profile(profile([job("2025-11", None, True)]), as_of=date(2026, 11, 3))
    assert result.experience_months == 12


def test_invalid_dates_require_review() -> None:
    with pytest.raises(ProviderError, match="INVALID_EMPLOYMENT_DATES"):
        normalize_profile(profile([job("2025-01", "2020-01")]), as_of=date(2026, 11, 3))
