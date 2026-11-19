from pydantic import BaseModel, ConfigDict, Field

from recruitment.domain.scoring_contracts import ScoreBreakdown, ScoringRubric


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StrictAIModel(StrictModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)


class WorkHistory(StrictAIModel):
    company: str = Field(max_length=500)
    title: str = Field(max_length=500)
    years: float = Field(ge=0, le=80)
    summary: str = Field(max_length=2000)


class CandidateProfile(StrictAIModel):
    name: str = Field(max_length=200)
    email: str = Field(max_length=320)
    phone: str = Field(max_length=40)
    total_experience_years: float = Field(ge=0, le=80)
    skills: list[str] = Field(max_length=100)
    technologies: list[str] = Field(max_length=100)
    languages: list[str] = Field(max_length=30)
    education: list[str] = Field(max_length=30)
    work_history: list[WorkHistory] = Field(max_length=40)
    certifications: list[str] = Field(max_length=50)
    summary: str = Field(max_length=2000)


class JobDescription(StrictAIModel):
    title: str
    description: str
    requirements: str
    min_experience_years: float = Field(ge=0, le=80)
    relevant_experience_years: float | None = Field(ge=0, le=80)
    domain_experience: str
    rubric: ScoringRubric | None = None


class MatchResult(StrictAIModel):
    fit: bool
    score: float = Field(ge=0, le=100)
    matched: list[str] = Field(max_length=50)
    missing: list[str] = Field(max_length=50)
    exp_ok: bool
    reason: str = Field(min_length=1, max_length=2000)
    score_breakdown: ScoreBreakdown | None = None


class BriefMatchResult(MatchResult):
    matched: list[str] = Field(max_length=5)
    missing: list[str] = Field(max_length=5)
    reason: str = Field(min_length=1, max_length=600)


class Page(StrictModel):
    number: int | None
    text: str


class Extraction(StrictModel):
    pages: list[Page]
    method: str
    confidence: float = Field(default=1, ge=0, le=1)
