from typing import Protocol

from recruitment.domain.contracts import CandidateProfile, EvidenceGrade, EvidenceInput, Extraction
from recruitment.domain.experience import ExperienceFacts, ExperienceSource
from recruitment.domain.full_text import FullTextDocument, FullTextResult, FullTextRubric
from recruitment.domain.openings import GeneratedRequirements, RequirementSource


class FileStorage(Protocol):
    def store(self, content: bytes, mime_type: str) -> str: ...
    def read(self, file_id: str) -> bytes: ...


class DocumentExtractor(Protocol):
    def extract(self, content: bytes, mime_type: str) -> Extraction: ...


class DocumentConverter(Protocol):
    version: str

    def convert(self, content: bytes, mime_type: str) -> bytes: ...


class MalwareScanner(Protocol):
    def scan(self, content: bytes) -> None: ...


class OcrProvider(Protocol):
    def extract(self, content: bytes) -> Extraction: ...


class CandidateProfileExtractor(Protocol):
    model: str

    def profile(self, text: str) -> CandidateProfile: ...


class JobCriteriaExtractor(Protocol):
    model: str

    def requirements(self, sources: list[RequirementSource]) -> GeneratedRequirements: ...


class ExperienceEvidenceExtractor(Protocol):
    model: str

    def extract(
        self, sources: list[ExperienceSource], opening_ids: list[str]
    ) -> ExperienceFacts: ...


class EmbeddingProvider(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class MatchingGrader(Protocol):
    model: str

    def grade(self, criterion: str, chunks: list[EvidenceInput]) -> EvidenceGrade: ...


class FullTextGrader(Protocol):
    model: str

    def grade(self, rubric: FullTextRubric, document: FullTextDocument) -> FullTextResult: ...


class EmailProvider(Protocol):
    def send(self, *, idempotency_key: str, recipient: str, template: str) -> str: ...


class WhatsAppProvider(Protocol):
    def send_template(self, *, idempotency_key: str, template: str, recipient: str) -> str: ...


class CalendarProvider(Protocol):
    def busy_periods(self, calendar_id: str, start: str, end: str) -> list[tuple[str, str]]: ...


class TelephonyProvider(Protocol):
    def verified_connection(self, tenant_id: str) -> str: ...


class VoiceAgentProvider(Protocol):
    def place_call(self, *, idempotency_key: str, snapshot_id: str) -> str: ...
