from typing import Any, Protocol

from recruitment.domain.contracts import CandidateProfile, Extraction, JobDescription, MatchResult


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


class CandidateProfileExtractor(Protocol):
    model: str

    def profile(self, text: str) -> CandidateProfile: ...


class JobMatcher(Protocol):
    model: str

    def match(self, job: JobDescription, profile: dict[str, Any]) -> MatchResult: ...
