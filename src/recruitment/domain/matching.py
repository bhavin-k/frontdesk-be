import re
from typing import Any

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import CandidateProfile, JobDescription, MatchResult

_CONTACT_TEXT = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|\+?\d(?:[\d ()-]{8,}\d)", re.IGNORECASE)
EXPERIENCE_FAILURE_SCORE_CAP = 14


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
    if not result.reason.strip():
        raise ProviderError("INVALID_MATCH_OUTPUT")
    score = round(result.score, 2)
    reason = result.reason
    skills = f"Matches {', '.join(result.matched[:3])[:500]}. " if result.matched else ""
    experience = (
        f"Required {job.min_experience_years:g} years; "
        f"CV documents {profile.total_experience_years:g} years."
    )
    if not exp_ok:
        score = min(score, EXPERIENCE_FAILURE_SCORE_CAP)
        if result.score_breakdown is None:
            reason = (
                f"{skills}Experience requirement not met. {experience} "
                f"The score is capped at {EXPERIENCE_FAILURE_SCORE_CAP} "
                "because the minimum is unmet."
            )
    elif result.exp_ok != exp_ok:
        gaps = (
            f" Undocumented requirements include {', '.join(result.missing[:3])[:500]}."
            if result.missing
            else ""
        )
        reason = f"{skills}Experience minimum met. {experience}{gaps}"
    return MatchResult.model_validate(
        {
            **result.model_dump(),
            "fit": result.fit and exp_ok,
            "exp_ok": exp_ok,
            "score": score,
            "reason": reason,
        }
    )
