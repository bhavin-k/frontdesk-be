import unicodedata
from contextlib import suppress
from typing import Any

import phonenumbers
from email_validator import EmailNotValidError, validate_email

from recruitment.domain.contracts import CandidateProfile


def clean(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def normalize_profile(profile: CandidateProfile) -> CandidateProfile:
    email, phone = "", ""
    if profile.email:
        with suppress(EmailNotValidError):
            email = validate_email(profile.email, check_deliverability=False).normalized.casefold()
    if profile.phone:
        try:
            parsed = phonenumbers.parse(profile.phone, "IN")
            if phonenumbers.is_valid_number(parsed):
                phone = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
        except phonenumbers.NumberParseException:
            pass
    return profile.model_copy(update={"name": clean(profile.name), "email": email, "phone": phone})


def with_contacts(profile: dict[str, Any], contacts: dict[str, Any]) -> dict[str, Any]:
    if "phone" not in profile:
        return {**profile, **contacts}
    return {
        **profile,
        **{("phone" if key == "mobile" else key): value or "" for key, value in contacts.items()},
    }
