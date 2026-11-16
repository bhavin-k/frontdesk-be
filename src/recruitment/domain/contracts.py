from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Grade = Literal["met", "partial", "missing", "unknown"]
Recommendation = Literal["recommended_shortlist", "recommended_rejection", "needs_review"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Criterion(StrictModel):
    text: str = Field(min_length=3, max_length=500)
    required: bool = True
    strict: bool = False
    weight: float = Field(default=1, gt=0, le=100)


class Employment(StrictModel):
    company: str | None
    title: str | None
    start_month: str | None = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    end_month: str | None = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    current: bool
    description: str
    duration_months: int | None


class Education(StrictModel):
    institution: str | None
    qualification: str | None
    field_of_study: str | None
    completion_year: int | None


class CandidateProfile(StrictModel):
    name: str | None
    email: str | None
    mobile: str | None
    language: Literal["en", "other", "unknown"]
    skills: list[str]
    employment: list[Employment]
    education: list[Education]
    certifications: list[str]
    projects: list[str]
    confidence: float = Field(ge=0, le=1)
    location: str | None = None
    experience_months: int | None = None


class EvidenceInput(StrictModel):
    chunk_id: str
    text: str


class EvidenceGrade(StrictModel):
    status: Grade
    confidence: float = Field(ge=0, le=1)
    evidence_chunk_ids: list[str]
    quotes: list[str]
    reason: str = Field(min_length=1, max_length=1000)


class Page(StrictModel):
    number: int | None
    text: str


class Extraction(StrictModel):
    pages: list[Page]
    method: str
    confidence: float = Field(default=1, ge=0, le=1)
