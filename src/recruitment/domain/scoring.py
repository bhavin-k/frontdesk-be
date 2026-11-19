import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from typing import Any, Literal

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import JobDescription, MatchResult
from recruitment.domain.scoring_contracts import (
    POLICY_VERSION,
    AdditionalSkill,
    ChecklistDraft,
    Criterion,
    CriterionResult,
    Evidence,
    EvidenceChoices,
    EvidenceGrades,
    ScoreBreakdown,
    ScoringRubric,
)

CREDIT = {"met": Decimal(1), "partial": Decimal("0.5"), "undocumented": Decimal(0)}
CURVE = [(0, 0), (50, 50), (75, 81), (90, 90), (100, 96)]
PROFILE_FIELDS = {
    "skills",
    "technologies",
    "summary",
    "work_history",
    "education",
    "certifications",
    "languages",
}


def profile_evidence(profile: dict[str, Any]) -> dict[str, Evidence]:
    sources: dict[str, Evidence] = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, str) and value.strip():
            remaining = value
            while remaining.strip():
                cut = len(remaining) if len(remaining) <= 240 else remaining.rfind(" ", 0, 240)
                if cut < 1:
                    cut = min(240, len(remaining))
                quote, remaining = remaining[:cut], remaining[cut:]
                if quote.strip():
                    sources[f"e{len(sources) + 1}"] = Evidence(path=path, quote=quote)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}/{index}")
        elif isinstance(value, dict):
            for key, child in value.items():
                visit(child, f"{path}/{key}")

    for key, value in profile.items():
        if key in PROFILE_FIELDS:
            visit(value, key)
    return sources


def resolve_grades(choices: EvidenceChoices, sources: dict[str, Evidence]) -> EvidenceGrades:
    def resolve(key: str | None) -> Evidence | None:
        if key is None:
            return None
        if key not in sources:
            raise ProviderError("INVALID_MATCH_EVIDENCE")
        return sources[key]

    extras = []
    for row in choices.additional_skills:
        source = resolve(row.id)
        assert source is not None
        if source.path.split("/")[0] not in {"skills", "technologies"}:
            raise ProviderError("INVALID_MATCH_EVIDENCE")
        extras.append(
            {"skill": source.quote[:100], "related_to": row.related_to, "evidence": source}
        )

    return EvidenceGrades.model_validate(
        {
            "grades": [
                {**row.model_dump(), "evidence": resolve(row.evidence)} for row in choices.grades
            ],
            "additional_skills": extras,
        }
    )


def signature(job: JobDescription) -> str:
    source = json.dumps(
        {"policy": POLICY_VERSION, "jd": job.model_dump(exclude={"rubric"})},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(source.encode()).hexdigest()


def skill_key(value: str) -> str:
    return " ".join(re.findall(r"[\w+#.]+", unicodedata.normalize("NFKC", value).casefold()))


def checklist(job: JobDescription, draft: ChecklistDraft) -> ScoringRubric:
    rows: list[Criterion] = []
    seen = set()
    groups: list[tuple[Literal["required", "preferred"], list[str]]] = [
        ("required", draft.required),
        ("preferred", draft.preferred),
    ]
    for kind, labels in groups:
        for label in labels:
            label = " ".join(label.split())
            key = skill_key(label)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(Criterion(id=f"c{len(rows) + 1}", label=label, kind=kind))
    return ScoringRubric(signature=signature(job), criteria=rows)


def supported(profile: dict[str, Any], evidence: Evidence | None) -> bool:
    if evidence is None:
        return False
    path = evidence.path.removeprefix("/").split("/")
    if path[0] not in PROFILE_FIELDS:
        return False
    value: Any = profile
    for part in path:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdecimal() and int(part) < len(value):
            value = value[int(part)]
        else:
            return False
    return (
        isinstance(value, str)
        and bool(evidence.quote.strip())
        and (" ".join(evidence.quote.casefold().split()) in " ".join(value.casefold().split()))
    )


def base_score(coverage: Decimal) -> Decimal:
    for (left, low), (right, high) in zip(CURVE, CURVE[1:], strict=False):
        if coverage <= right:
            return Decimal(low) + (coverage - left) * Decimal(high - low) / Decimal(right - left)
    return Decimal(96)


def calculate_match(
    job: JobDescription, profile: dict[str, Any], assessment: EvidenceGrades
) -> MatchResult:
    rubric = job.rubric
    if rubric is None or rubric.signature != signature(job):
        raise ProviderError("INVALID_MATCH_RUBRIC")
    grades = {grade.id: grade for grade in assessment.grades}
    if len(grades) != len(assessment.grades) or set(grades) != {row.id for row in rubric.criteria}:
        raise ProviderError("INVALID_CRITERION_GRADES")
    results = []
    for criterion in rubric.criteria:
        grade = grades[criterion.id]
        if grade.status != "undocumented" and not supported(profile, grade.evidence):
            raise ProviderError("INVALID_MATCH_EVIDENCE")
        results.append(
            CriterionResult(
                **grade.model_dump(),
                label=criterion.label,
                kind=criterion.kind,
                credit=float(CREDIT[grade.status]),
            )
        )
    required = [row for row in results if row.kind == "required"]
    preferred = [row for row in results if row.kind == "preferred"]
    coverage = sum((CREDIT[row.status] for row in required), Decimal(0)) * 100 / len(required)
    preferred_bonus = (
        2 * sum((CREDIT[row.status] for row in preferred), Decimal(0)) / len(preferred)
        if preferred
        else Decimal(0)
    )
    accepted: list[AdditionalSkill] = []
    known = [set(skill_key(row.label).split()) for row in rubric.criteria]
    seen: list[set[str]] = []
    for extra in assessment.additional_skills:
        if extra.related_to not in grades or not supported(profile, extra.evidence):
            raise ProviderError("INVALID_MATCH_EVIDENCE")
        tokens = set(skill_key(extra.skill).split())
        if (
            not tokens
            or any(tokens <= item or item <= tokens for item in known + seen)
            or any(row.evidence.path == extra.evidence.path for row in accepted)
        ):
            continue
        seen.append(tokens)
        accepted.append(extra)
    additional_bonus = Decimal(len(accepted))
    base = base_score(coverage)
    score = min(Decimal(100), base + preferred_bonus + additional_bonus)
    if any(row.status != "met" for row in required):
        score = min(Decimal(94), score)
    documented = float(profile["total_experience_years"])
    exp_ok = documented >= job.min_experience_years
    if not exp_ok:
        score = min(Decimal(14), score)
    missing = [f"{row.label} ({row.status})" for row in required if row.status != "met"]
    missing.extend(
        f"{row.label} (optional, {row.status})" for row in preferred if row.status != "met"
    )
    matched = [row.label for row in results if row.status == "met"]
    explanation = (
        f"Required-skill coverage {coverage:.2f}%. Base {base:.2f} plus "
        f"{preferred_bonus + additional_bonus:.2f} bonus points. "
        f"Required {job.min_experience_years:g} years; CV documents {documented:g} years."
    )
    if missing:
        explanation += " Missing or partial: " + ", ".join(missing[:3])[:700] + "."
    else:
        explanation += " All required skills have documented evidence."
    if not exp_ok:
        explanation += " Experience requirement not met; score capped at 14."
    breakdown = ScoreBreakdown(
        rubric_signature=rubric.signature,
        coverage_percent=float(round(coverage, 2)),
        base_score=float(round(base, 2)),
        preferred_bonus=float(round(preferred_bonus, 2)),
        additional_bonus=float(additional_bonus),
        experience_required=job.min_experience_years,
        experience_documented=documented,
        criteria=results,
        additional_skills=accepted,
    )
    return MatchResult(
        fit=exp_ok and coverage >= 75,
        score=float(round(score, 2)),
        matched=matched,
        missing=missing,
        exp_ok=exp_ok,
        reason=explanation,
        score_breakdown=breakdown,
    )
