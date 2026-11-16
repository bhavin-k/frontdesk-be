"""Deterministic development/test providers. These are not candidate assessment models."""

import hashlib
import math
import re

from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import (
    CandidateProfile,
    EvidenceGrade,
    EvidenceInput,
    Extraction,
    Page,
)
from recruitment.domain.full_text import (
    FullTextDocument,
    FullTextGrade,
    FullTextResponse,
    FullTextResult,
    FullTextRubric,
    QuoteSelection,
    verify_full_text,
)
from recruitment.domain.openings import (
    ExtractedRequirement,
    GeneratedRequirements,
    RequirementSource,
)


class FakeJobCriteria:
    model = "fake-job-criteria-v1"

    def requirements(self, sources: list[RequirementSource]) -> GeneratedRequirements:
        criteria = []
        for source in sources:
            # Keep exact source text for evidence; fixtures are deterministic, not assessments.
            for line in re.split(r"[\n;]+", source.text):
                line = line.strip()
                if len(line) >= 3:
                    criteria.append(
                        ExtractedRequirement(
                            text=line[:500],
                            required=not bool(re.search(r"\b(preferred|optional)\b", line, re.I)),
                            strict=bool(
                                re.search(r"\b(must|mandatory|non-negotiable)\b", line, re.I)
                            ),
                            source_id=source.id,
                            quote=line[:2000],
                        )
                    )
        return GeneratedRequirements(criteria=criteria[:30])


class FakeScanner:
    def scan(self, content: bytes) -> None:
        if b"EICAR" in content:
            raise ProviderError("UNSAFE_FILE")


class FakeOCR:
    def __init__(
        self, text: str = ("Fixture Candidate\nfixture@example.org\nPython development experience.")
    ) -> None:
        self.text = text
        self.calls = 0

    def extract(self, content: bytes) -> Extraction:
        self.calls += 1
        return Extraction(
            pages=[Page(number=1, text=self.text)], method="fake-ocr", confidence=0.99
        )


class FakeProfiles:
    model = "fake-profile-v1"

    def profile(self, text: str) -> CandidateProfile:
        email = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
        phone = re.search(r"\+91[ -]?[6-9][0-9 -]{9,12}", text)
        return CandidateProfile(
            name=text.splitlines()[0][:100] or None,
            email=email[0] if email else None,
            mobile=phone[0] if phone else None,
            language="other" if re.search(r"[\u0900-\u097f]", text) else "en",
            skills=[
                skill for skill in ("Python", "PostgreSQL", "SQL") if skill.lower() in text.lower()
            ],
            employment=[],
            education=[],
            certifications=[],
            projects=[],
            confidence=0.99,
        )


class FakeEmbeddings:
    model = "fake-embedding-v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * 1536
            for token in re.findall(r"\w+", text.lower()):
                index = int.from_bytes(hashlib.sha256(token.encode()).digest()[:4]) % 1536
                vector[index] += 1.0
            if not any(vector):
                vector[0] = 1.0
            norm = math.sqrt(sum(value * value for value in vector))
            vectors.append([value / norm for value in vector])
        return vectors


class FakeGrader:
    model = "fake-grader-v1"

    def __init__(self) -> None:
        self.overrides: dict[str, EvidenceGrade] = {}

    def grade(self, criterion: str, chunks: list[EvidenceInput]) -> EvidenceGrade:
        if criterion in self.overrides:
            return self.overrides[criterion]
        words = re.findall(r"\w+", criterion.lower())
        for chunk in chunks:
            line = next(
                (
                    line.strip()
                    for line in chunk.text.splitlines()
                    if any(word in line.lower() for word in words if len(word) >= 3)
                    and line.strip()
                ),
                None,
            )
            if line:
                return EvidenceGrade(
                    status="met",
                    confidence=0.99,
                    evidence_chunk_ids=[chunk.chunk_id],
                    quotes=[line],
                    reason="The fixture contains matching text.",
                )
        return EvidenceGrade(
            status="unknown",
            confidence=0.99,
            evidence_chunk_ids=[],
            quotes=[],
            reason="The fixture does not establish this requirement.",
        )


class FakeFullTextGrader:
    model = "fake-full-text-grader-v1"

    def grade(self, rubric: FullTextRubric, document: FullTextDocument) -> FullTextResult:
        grader = FakeGrader()
        sources = [
            EvidenceInput(chunk_id=source.id, text=source.masked) for source in document.sources
        ]
        grades = []
        for criterion in rubric.criteria:
            result = grader.grade(criterion.text, sources)
            grades.append(
                FullTextGrade(
                    criterion_id=criterion.criterion_id,
                    status=result.status,
                    confidence=result.confidence,
                    reason=result.reason,
                    citations=[
                        QuoteSelection(source_id=source_id, quote=quote[:600])
                        for source_id, quote in zip(
                            result.evidence_chunk_ids, result.quotes, strict=True
                        )
                    ],
                )
            )
        return verify_full_text(rubric, document, FullTextResponse(grades=grades))


class RecordingDeliveryFake:
    """Inert implementation for future email, WhatsApp, calendar and voice contract tests."""

    def __init__(self) -> None:
        self.deliveries: dict[str, str] = {}

    def send(self, *, idempotency_key: str, recipient: str, template: str) -> str:
        return self.deliveries.setdefault(idempotency_key, f"fake-{len(self.deliveries) + 1}")

    def send_template(self, *, idempotency_key: str, template: str, recipient: str) -> str:
        return self.send(idempotency_key=idempotency_key, recipient=recipient, template=template)

    def busy_periods(self, calendar_id: str, start: str, end: str) -> list[tuple[str, str]]:
        return []

    def verified_connection(self, tenant_id: str) -> str:
        return "fake-disabled-connection"

    def place_call(self, *, idempotency_key: str, snapshot_id: str) -> str:
        raise ProviderError("VOICE_NOT_ENABLED")
