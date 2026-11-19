from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

POLICY_VERSION: Literal["coverage-v1"] = "coverage-v1"
RUBRIC_PROMPT = "jd-checklist-v1"
GRADE_PROMPT = "jd-evidence-v1"


class ScoringModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ChecklistDraft(ScoringModel):
    required: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(
        min_length=1, max_length=16
    )
    preferred: list[Annotated[str, Field(min_length=1, max_length=240)]] = Field(max_length=8)


class Criterion(ScoringModel):
    id: str = Field(min_length=1, max_length=12)
    label: str = Field(min_length=1, max_length=240)
    kind: Literal["required", "preferred"]


class ScoringRubric(ScoringModel):
    version: Literal["coverage-v1"] = POLICY_VERSION
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    criteria: list[Criterion] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def validate_criteria(self) -> "ScoringRubric":
        if len({row.id for row in self.criteria}) != len(self.criteria):
            raise ValueError("Checklist criterion IDs must be unique.")
        if not any(row.kind == "required" for row in self.criteria):
            raise ValueError("A checklist needs a required criterion.")
        return self


class Evidence(ScoringModel):
    path: str = Field(min_length=1, max_length=100)
    quote: str = Field(min_length=1, max_length=240)


class CriterionGrade(ScoringModel):
    id: str = Field(min_length=1, max_length=12)
    status: Literal["met", "partial", "undocumented"]
    evidence: Evidence | None


class AdditionalSkill(ScoringModel):
    skill: str = Field(min_length=1, max_length=100)
    related_to: str = Field(min_length=1, max_length=12)
    evidence: Evidence


class EvidenceGrades(ScoringModel):
    grades: list[CriterionGrade] = Field(min_length=1, max_length=24)
    additional_skills: list[AdditionalSkill] = Field(max_length=2)


class EvidenceChoice(ScoringModel):
    id: str = Field(min_length=1, max_length=12)
    status: Literal["met", "partial", "undocumented"]
    evidence: str | None


class AdditionalChoice(ScoringModel):
    id: str = Field(min_length=1, max_length=12)
    related_to: str = Field(min_length=1, max_length=12)


class EvidenceChoices(ScoringModel):
    grades: list[EvidenceChoice] = Field(min_length=1, max_length=24)
    additional_skills: list[AdditionalChoice] = Field(max_length=2)


class CriterionResult(CriterionGrade):
    label: str
    kind: Literal["required", "preferred"]
    credit: float


class ScoreBreakdown(ScoringModel):
    policy_version: Literal["coverage-v1"] = POLICY_VERSION
    rubric_signature: str
    coverage_percent: float
    base_score: float
    preferred_bonus: float
    additional_bonus: float
    experience_required: float
    experience_documented: float
    criteria: list[CriterionResult]
    additional_skills: list[AdditionalSkill]
