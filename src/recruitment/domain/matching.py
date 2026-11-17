import re
from typing import Any

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import CandidateProfile, JobDescription, MatchResult

_CONTACT_TEXT = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+?\d(?:[\d ()-]{8,}\d)", re.IGNORECASE)


def masked_profile(profile: CandidateProfile) -> dict[str, Any]:
    values = [value for value in (profile.name, profile.email, profile.phone) if value]
    identities = (
        re.compile(r"(?<!\w)(?:" + "|".join(re.escape(v) for v in values) + r")(?!\w)", re.I)
        if values
        else None
    )

    def mask(value: Any) -> Any:
        if isinstance(value, str):
            value = identities.sub("[redacted]", value) if identities else value
            return _CONTACT_TEXT.sub("[redacted]", value)
        if isinstance(value, list):
            return [mask(item) for item in value]
        if isinstance(value, dict):
            return {key: mask(item) for key, item in value.items()}
        return value

    return {**mask(profile.model_dump()), "name": "", "email": "", "phone": ""}


def checked_match(
    job: JobDescription, profile: CandidateProfile, result: MatchResult
) -> MatchResult:
    exp_ok = profile.total_experience_years >= job.min_experience_years
    if result.exp_ok != exp_ok or (result.fit and not exp_ok) or not result.reason.strip():
        raise ProviderError("INVALID_MATCH_OUTPUT")
    return result.model_copy(update={"score": round(result.score, 2)})
