import unicodedata
from datetime import date

import phonenumbers
from email_validator import EmailNotValidError, validate_email

from recruitment.core.errors import ProviderError
from recruitment.core.time import utcnow
from recruitment.domain.contracts import CandidateProfile


def clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def month_number(value: str) -> int:
    year, month = value.split("-")
    return int(year) * 12 + int(month) - 1


def normalize_profile(profile: CandidateProfile, as_of: date | None = None) -> CandidateProfile:
    email, mobile = profile.email, profile.mobile
    try:
        if email:
            email = validate_email(email, check_deliverability=False).normalized.casefold()
        if mobile:
            parsed = phonenumbers.parse(mobile, "IN")
            mobile = (
                phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
                if phonenumbers.is_valid_number(parsed)
                else None
            )
    except (EmailNotValidError, phonenumbers.NumberParseException) as exc:
        raise ProviderError("INVALID_PROFILE_CONTACT") from exc
    aliases = {"postgres": "PostgreSQL", "postgresql": "PostgreSQL", "python": "Python"}
    skills = sorted(
        {aliases.get(clean(skill).casefold(), clean(skill)) for skill in profile.skills}
    )
    as_of = as_of or utcnow().date()
    current_month = as_of.year * 12 + as_of.month - 1
    intervals: list[tuple[int, int]] = []
    employment = []
    complete = True
    for item in profile.employment:
        start = month_number(item.start_month) if item.start_month else None
        end = (
            current_month
            if item.current
            else (month_number(item.end_month) if item.end_month else None)
        )
        if (item.current and item.end_month) or (
            start is not None and end is not None and (end < start or end > current_month)
        ):
            raise ProviderError("INVALID_EMPLOYMENT_DATES")
        duration = end - start if start is not None and end is not None else None
        if duration is None:
            complete = False
        else:
            assert start is not None and end is not None
            intervals.append((start, end))
        employment.append(item.model_copy(update={"duration_months": duration}))
    # Count overlapping employment months once, rather than adding concurrent jobs.
    merged: list[tuple[int, int]] = []
    for begin, finish in sorted(intervals):
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(finish, merged[-1][1]))
        else:
            merged.append((begin, finish))
    experience = (
        sum(finish - begin for begin, finish in merged) if complete and employment else None
    )
    return profile.model_copy(
        update={
            "name": clean(profile.name) if profile.name else None,
            "email": email,
            "mobile": mobile,
            "skills": skills,
            "employment": employment,
            "location": clean(profile.location) if profile.location else None,
            "experience_months": experience,
        }
    )
