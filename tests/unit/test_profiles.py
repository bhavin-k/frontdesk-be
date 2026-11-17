import pytest
from pydantic import ValidationError

from recruitment.adapters.fakes import FakeProfiles
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import CandidateProfile, JobDescription, MatchResult
from recruitment.domain.matching import checked_match, masked_profile
from recruitment.domain.profiles import normalize_profile


def test_profile_is_strict_and_contact_details_are_masked():
    profile = FakeProfiles().profile("Example Candidate\nEXAMPLE@example.org\n+91 98765 43210")
    profile.summary = "Example Candidate at EXAMPLE@example.org, phone +91 98765 43210"
    profile = normalize_profile(profile)
    assert profile.email == "example@example.org" and profile.phone == "+919876543210"
    assert all(masked_profile(profile)[key] == "" for key in ("name", "email", "phone"))
    assert "Example Candidate" not in masked_profile(profile)["summary"]
    assert "98765" not in masked_profile(profile)["summary"]
    invalid = {**profile.model_dump(), "total_experience_years": "4"}
    with pytest.raises(ValidationError):
        CandidateProfile.model_validate(invalid)


def test_match_requires_consistent_experience():
    job = JobDescription(
        title="Developer",
        description="Python development",
        requirements="",
        min_experience_years=2,
        relevant_experience_years=None,
        domain_experience="",
    )
    profile = FakeProfiles().profile("Fictional\n1 year of Python")
    with pytest.raises(ValidationError):
        MatchResult(fit=True, score=101, matched=[], missing=[], exp_ok=False, reason="Invalid")
    with pytest.raises(ProviderError, match="INVALID_MATCH_OUTPUT"):
        checked_match(
            job,
            profile,
            MatchResult(
                fit=True,
                score=100,
                matched=["Python"],
                missing=[],
                exp_ok=False,
                reason="Invalid",
            ),
        )
