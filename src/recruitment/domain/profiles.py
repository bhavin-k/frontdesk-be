import unicodedata
from contextlib import suppress
from typing import Any

import phonenumbers
from email_validator import EmailNotValidError, validate_email

from recruitment.domain.contracts import CandidateProfile


def clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def normalize_email(value: str) -> str:
    if value:
        with suppress(EmailNotValidError):
            return validate_email(value, check_deliverability=False).normalized.casefold()
    return ""


def normalize_phone(value: str, region: str | None = "IN") -> str:
    if value:
        try:
            parsed = phonenumbers.parse(value, region)
            if phonenumbers.is_valid_number(parsed):
                return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
    return ""


def normalize_profile(profile: CandidateProfile) -> CandidateProfile:
    return profile.model_copy(
        update={
            "name": clean(profile.name),
            "email": normalize_email(profile.email),
            "phone": normalize_phone(profile.phone),
        }
    )


def with_contacts(profile: dict[str, Any], contacts: dict[str, Any]) -> dict[str, Any]:
    if "phone" not in profile:
        return {**profile, **contacts}
    return {
        **profile,
        **{("phone" if key == "mobile" else key): value or "" for key, value in contacts.items()},
    }
