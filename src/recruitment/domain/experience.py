"""Source-backed experience eligibility. Models supply facts; Python counts months."""

import calendar
import re
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Literal

from pydantic import Field

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import StrictModel
from recruitment.domain.profiles import month_number

POLICY_VERSION = "experience-first-v1"


class ExperienceSource(StrictModel):
    id: str
    text: str
    opening_id: str | None = None
    page: int | None = None
    char_start: int = 0
    char_end: int = 0


class ExperienceMinimum(StrictModel):
    kind: Literal["total", "relevant"]
    amount: Decimal = Field(ge=0, le=960)
    unit: Literal["years", "months"]
    required: bool = True
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    domain: str | None = Field(default=None, max_length=200)

    def months(self) -> int:
        value = self.amount * (12 if self.unit == "years" else 1)
        if value > 960:
            raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
        return int(value.to_integral_value(rounding=ROUND_CEILING))


class RoleExperience(StrictModel):
    opening_id: str
    requirements: list[ExperienceMinimum]
    conflicting: bool = False


class EmploymentEvidence(StrictModel):
    start_month: str | None = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    end_month: str | None = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    current: bool
    is_internship: bool | None
    relevant_opening_ids: list[str]
    irrelevant_opening_ids: list[str]
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str]
    relevant_domains: list[str] = Field(default_factory=list)
    irrelevant_domains: list[str] = Field(default_factory=list)


class ExperienceFacts(StrictModel):
    roles: list[RoleExperience]
    employment: list[EmploymentEvidence]
    complete_employment: bool


class Eligibility(StrictModel):
    status: Literal["passed", "failed", "unknown", "not_required"]
    professional_months: int
    relevant_months: int
    required_total_months: int
    required_relevant_months: int
    confidence: float
    reason: str
    evidence: list[ExperienceSource]
    checks: list[dict[str, str | int | None]] = Field(default_factory=list)


def union_months(intervals: list[tuple[int, int]]) -> int:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return sum(end - start for start, end in merged)


def date_supported(value: str, text: str) -> bool:
    year, month = value.split("-")
    normalized = re.sub(r"\s+", " ", text.lower())
    name = calendar.month_abbr[int(month)].lower()
    return any(
        re.search(pattern, normalized)
        for pattern in (
            rf"\b{year}\s*[-/]\s*0?{int(month)}(?!\d)",
            rf"(?<!\d)0?{int(month)}\s*[-/]\s*{year}\b",
            rf"\b{name}[a-z]*[\s,./-]*{year}(?!\d)",
        )
    )


def validate_facts(
    facts: ExperienceFacts, sources: list[ExperienceSource], opening_ids: list[str]
) -> None:
    by_id = {source.id: source for source in sources}
    allowed = set(opening_ids)
    if len(facts.roles) != len(allowed) or {role.opening_id for role in facts.roles} != allowed:
        raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
    for role in facts.roles:
        for requirement in role.requirements:
            requirement.months()
            if not requirement.evidence_ids or any(
                key not in by_id or by_id[key].opening_id != role.opening_id
                for key in requirement.evidence_ids
            ):
                raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
            text = " ".join(by_id[key].text for key in requirement.evidence_ids).lower()
            values = {Decimal(value) for value in re.findall(r"\b\d+(?:\.\d+)?\b", text)}
            words = {
                "one": 1,
                "two": 2,
                "three": 3,
                "four": 4,
                "five": 5,
                "six": 6,
                "seven": 7,
                "eight": 8,
                "nine": 9,
                "ten": 10,
            }
            values.update(
                Decimal(value) for word, value in words.items() if re.search(rf"\b{word}\b", text)
            )
            unit = r"\b(years?|yrs?)\b" if requirement.unit == "years" else r"\bmonths?\b"
            if requirement.amount not in values or not re.search(unit, text):
                raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
    for job in facts.employment:
        if not job.evidence_ids or any(
            key not in by_id or by_id[key].opening_id is not None for key in job.evidence_ids
        ):
            raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
        text = "\n".join(by_id[key].text for key in job.evidence_ids)
        if any(
            not date_supported(value, text) for value in (job.start_month, job.end_month) if value
        ):
            raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
        if job.current and not re.search(r"\b(present|current|ongoing|now)\b", text, re.I):
            raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
        if job.is_internship is False and re.search(r"\bintern(ship)?\b", text, re.I):
            raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
        if job.is_internship is True and not re.search(r"\bintern(ship)?\b", text, re.I):
            raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")
        relevant, irrelevant = set(job.relevant_opening_ids), set(job.irrelevant_opening_ids)
        if not (relevant | irrelevant) <= allowed or relevant & irrelevant:
            raise ProviderError("INVALID_EXPERIENCE_EVIDENCE")


def eligibility(
    role: RoleExperience,
    facts: ExperienceFacts,
    sources: list[ExperienceSource],
    as_of: date,
    profile_confidence: float,
) -> Eligibility:
    requirements = [item for item in role.requirements if item.required]
    total = max((item.months() for item in requirements if item.kind == "total"), default=0)
    relevant = max((item.months() for item in requirements if item.kind == "relevant"), default=0)
    now = as_of.year * 12 + as_of.month - 1
    jobs: list[tuple[EmploymentEvidence, tuple[int, int]]] = []
    uncertain = not facts.complete_employment or not facts.employment
    confidence = min([profile_confidence, *[item.confidence for item in requirements]])
    used = {key for item in requirements for key in item.evidence_ids}
    for job in facts.employment:
        if job.is_internship is True:
            continue
        used.update(job.evidence_ids)
        confidence = min(confidence, job.confidence)
        start = month_number(job.start_month) if job.start_month else None
        end = now if job.current else (month_number(job.end_month) if job.end_month else None)
        if (
            job.is_internship is None
            or start is None
            or end is None
            or end < start
            or end > now
            or (job.current and job.end_month)
        ):
            uncertain = True
        else:
            jobs.append((job, (start, end)))
    months = union_months([interval for _, interval in jobs])
    role_months = union_months(
        [interval for job, interval in jobs if role.opening_id in job.relevant_opening_ids]
    )
    checks: list[dict[str, str | int | None]] = []
    for requirement in requirements:
        minimum = requirement.months()
        ambiguous = uncertain
        if requirement.kind == "total":
            counted = months
        else:
            intervals = []
            for job, interval in jobs:
                if requirement.domain:
                    domain = requirement.domain.casefold()
                    matched = domain in {value.casefold() for value in job.relevant_domains}
                    excluded = domain in {value.casefold() for value in job.irrelevant_domains}
                else:
                    matched = role.opening_id in job.relevant_opening_ids
                    excluded = role.opening_id in job.irrelevant_opening_ids
                if matched:
                    intervals.append(interval)
                elif not excluded:
                    ambiguous = True
            counted = union_months(intervals)
        if counted >= minimum:
            outcome = "passed"
        elif months < minimum and not uncertain:
            # Professional time is a firm upper bound on time in any specific domain.
            counted, outcome = months, "failed"
        else:
            outcome = "unknown" if ambiguous else "failed"
        checks.append(
            {
                "kind": requirement.kind,
                "domain": requirement.domain,
                "counted_months": counted,
                "required_months": minimum,
                "status": outcome,
            }
        )
    status: Literal["passed", "failed", "unknown", "not_required"]
    failed = next((item for item in checks if item["status"] == "failed"), None)
    if not requirements and not role.conflicting:
        status, reason = "not_required", "This role has no required minimum experience."
    elif role.conflicting or confidence < 0.8:
        status, reason = "unknown", "Experience evidence or job requirements need verification."
    elif failed:
        status = "failed"
        reason = (
            f"CV documents {failed['counted_months']} months of professional experience; "
            f"this role requires {failed['required_months']} months. Internships are excluded."
        )
        if failed["domain"] and months >= int(str(failed["required_months"])):
            reason = (
                f"CV documents {failed['counted_months']} months in {failed['domain']}; "
                f"this role requires {failed['required_months']} months. Internships are excluded."
            )
    elif any(item["status"] == "unknown" for item in checks):
        status, reason = (
            "unknown",
            "Dates, employment type or relevant experience could not be verified.",
        )
    else:
        status, reason = "passed", "Documented professional experience meets the minimum."
    return Eligibility(
        status=status,
        professional_months=months,
        relevant_months=role_months,
        required_total_months=total,
        required_relevant_months=relevant,
        confidence=confidence,
        reason=reason,
        evidence=[source for source in sources if source.id in used],
        checks=checks,
    )
