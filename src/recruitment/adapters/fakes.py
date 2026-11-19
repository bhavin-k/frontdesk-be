"""Deterministic local providers for fictional CVs."""

import re
from typing import Any

import pymupdf

from recruitment.adapters.documents import PDF
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import (
    CandidateProfile,
    Extraction,
    JobDescription,
    MatchResult,
    Page,
)
from recruitment.domain.scoring import calculate_match
from recruitment.domain.scoring_contracts import (
    ChecklistDraft,
    CriterionGrade,
    Evidence,
    EvidenceGrades,
)


class FakeScanner:
    def scan(self, content: bytes) -> None:
        if b"EICAR" in content:
            raise ProviderError("UNSAFE_FILE")


class FakeOCR:
    def __init__(self, text: str | None = None) -> None:
        self.text = text
        self.calls = 0

    def extract(self, content: bytes, mime_type: str) -> Extraction:
        self.calls += 1
        if self.text is not None:
            pages = [Page(number=1, text=self.text)]
        elif mime_type == PDF:
            with pymupdf.open(stream=content, filetype="pdf") as document:  # type: ignore[no-untyped-call]
                pages = [Page(number=i, text=page.get_text()) for i, page in enumerate(document, 1)]
        else:
            pages = []
        if self.text is None and not any(page.text.strip() for page in pages):
            pages = [
                Page(
                    number=1,
                    text="Fixture Candidate\nfixture@example.org\nPython development experience.",
                )
            ]
        return Extraction(pages=pages, method="fake-ocr", confidence=0.99)


class FakeProfiles:
    model = "fake-profile-v1"

    def profile(self, text: str) -> CandidateProfile:
        email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        phone = re.search(r"\+91[ -]?[6-9][0-9 -]{9,12}", text)
        experience = re.search(r"(\d+(?:\.\d+)?)\s*(?:years?|yrs?)\b", text, re.I)
        return CandidateProfile(
            name=text.splitlines()[0][:100],
            email=email[0] if email else "",
            phone=phone[0] if phone else "",
            total_experience_years=min(80, float(experience[1])) if experience else 0,
            skills=[
                skill
                for skill in ("Python", "PostgreSQL", "SQL", "JavaScript", "Excel")
                if re.search(rf"\b{skill}\b", text, re.I)
            ],
            technologies=[],
            languages=[],
            work_history=[],
            education=[],
            certifications=[],
            summary="",
        )


class FakeMatcher:
    model = "fake-matcher-v1"

    def match(self, job: JobDescription, profile: dict[str, Any]) -> MatchResult:
        if job.rubric is not None:
            grades = []
            for criterion in job.rubric.criteria:
                source = next(
                    (
                        (index, skill)
                        for index, skill in enumerate(profile["skills"])
                        if skill.casefold() == criterion.label.casefold()
                    ),
                    None,
                )
                grades.append(
                    CriterionGrade(
                        id=criterion.id,
                        status="met" if source else "undocumented",
                        evidence=Evidence(path=f"skills/{source[0]}", quote=source[1])
                        if source
                        else None,
                    )
                )
            return calculate_match(
                job, profile, EvidenceGrades(grades=grades, additional_skills=[])
            )
        requirements = job.description + "\n" + job.requirements
        known = ("Python", "PostgreSQL", "SQL", "JavaScript", "Excel")
        expected = [name for name in known if re.search(rf"\b{name}\b", requirements, re.I)]
        matched = [name for name in expected if name in profile["skills"]]
        missing = [name for name in expected if name not in matched]
        exp_ok = profile["total_experience_years"] >= job.min_experience_years
        return MatchResult(
            fit=exp_ok and not missing,
            score=(100 * len(matched) / len(expected)) if expected else 100,
            matched=matched,
            missing=missing,
            exp_ok=exp_ok,
            reason="Development result from the fictional CV provider.",
        )


class FakeChecklistBuilder:
    model = "fake-checklist-v1"

    def prepare(self, job: JobDescription) -> ChecklistDraft:
        text = job.description + "\n" + job.requirements
        labels = [
            name
            for name in ("Python", "PostgreSQL", "SQL", "JavaScript", "Excel")
            if re.search(rf"\b{name}\b", text, re.I)
        ]
        return ChecklistDraft(required=labels or [job.title], preferred=[])
