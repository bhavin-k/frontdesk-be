from dataclasses import dataclass

from openai import DefaultHttpxClient, OpenAI

from recruitment.adapters.ai import CompatibleEmbeddings, LLMGrader, LLMJobCriteria, LLMProfiles
from recruitment.adapters.conversion import FakeDocumentConverter, GotenbergConverter
from recruitment.adapters.documents import ClamAVScanner, LocalExtractor
from recruitment.adapters.experience import EXPERIENCE_PROMPT_VERSION, FakeExperience, LLMExperience
from recruitment.adapters.fakes import (
    FakeEmbeddings,
    FakeFullTextGrader,
    FakeGrader,
    FakeJobCriteria,
    FakeOCR,
    FakeProfiles,
    FakeScanner,
)
from recruitment.adapters.full_text import LLMFullTextGrader
from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.adapters.ocr import TextractOCR
from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError
from recruitment.domain.full_text import FULL_TEXT_PROMPT_VERSION
from recruitment.domain.rag import embedding_fingerprint
from recruitment.ports.providers import (
    CandidateProfileExtractor,
    DocumentConverter,
    DocumentExtractor,
    EmbeddingProvider,
    ExperienceEvidenceExtractor,
    FullTextGrader,
    JobCriteriaExtractor,
    MalwareScanner,
    MatchingGrader,
    OcrProvider,
)


@dataclass
class Providers:
    scanner: MalwareScanner
    extractor: DocumentExtractor
    ocr: OcrProvider
    profiles: CandidateProfileExtractor
    embeddings: EmbeddingProvider
    grader: MatchingGrader
    name: str
    settings: Settings | None = None
    converter: DocumentConverter | None = None
    mailboxes: MailboxRuntime | None = None
    job_criteria: JobCriteriaExtractor | None = None
    experience: ExperienceEvidenceExtractor | None = None
    full_text_grader: FullTextGrader | None = None

    @property
    def fingerprint(self) -> str:
        settings = self.settings
        return embedding_fingerprint(
            "fake"
            if self.name == "fake"
            else (settings.embedding_provider if settings else self.name),
            "local://fake"
            if self.name == "fake"
            else (settings.embedding_base_url if settings else "https://api.openai.com/v1"),
            self.embeddings.model,
            1536,
            settings.embedding_revision if settings else "1",
        )

    def close(self) -> None:
        seen: set[int] = set()
        for adapter in (
            self.profiles,
            self.embeddings,
            self.grader,
            self.job_criteria,
            self.experience,
            self.full_text_grader,
        ):
            client = getattr(adapter, "client", None)
            if client is not None and id(client) not in seen:
                client.close()
                seen.add(id(client))

    @classmethod
    def from_settings(cls, settings: Settings) -> "Providers":
        if settings.provider_mode == "fake":
            return cls(
                FakeScanner(),
                LocalExtractor(),
                FakeOCR(),
                FakeProfiles(),
                FakeEmbeddings(),
                FakeGrader(),
                "fake",
                settings,
                FakeDocumentConverter(),
                job_criteria=FakeJobCriteria(),
                experience=FakeExperience(),
                full_text_grader=FakeFullTextGrader(),
            )
        llm_key = settings.llm_key()
        if not llm_key:
            raise ProviderError("LLM_NOT_CONFIGURED")
        embedding_key = settings.embedding_key()
        if not embedding_key:
            raise ProviderError("EMBEDDING_NOT_CONFIGURED")
        client = OpenAI(
            api_key=llm_key.get_secret_value(),
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
            http_client=DefaultHttpxClient(follow_redirects=False),
        )
        try:
            embedding_client = OpenAI(
                api_key=embedding_key.get_secret_value(),
                base_url=settings.embedding_base_url,
                timeout=settings.embedding_timeout_seconds,
                max_retries=0,
                http_client=DefaultHttpxClient(follow_redirects=False),
            )
        except Exception:
            client.close()
            raise
        return cls(
            ClamAVScanner(settings.clamav_host, settings.clamav_port),
            LocalExtractor(),
            TextractOCR(settings.aws_region),
            LLMProfiles(
                client,
                settings.profile_model,
                "profile-v1",
                settings.llm_api(),
                settings.llm_output_mode,
            ),
            CompatibleEmbeddings(
                embedding_client,
                settings.embedding_model,
                settings.embedding_batch_size,
                settings.embedding_max_batch_tokens,
                settings.embedding_max_parallel_calls,
                settings.embedding_send_dimensions,
            ),
            LLMGrader(
                client,
                settings.grader_model,
                "matching-v1",
                settings.llm_api(),
                settings.llm_output_mode,
                evidence_budget=settings.retrieval_context_tokens,
            ),
            settings.llm_provider,
            settings,
            GotenbergConverter(
                settings.document_converter_url, settings.document_conversion_timeout_seconds
            ),
            job_criteria=LLMJobCriteria(
                client,
                settings.profile_model,
                "job-criteria-v1",
                settings.llm_api(),
                settings.llm_output_mode,
            ),
            experience=LLMExperience(
                client,
                settings.profile_model,
                EXPERIENCE_PROMPT_VERSION,
                settings.llm_api(),
                settings.llm_output_mode,
                request_tokens=settings.experience_request_tokens,
                output_tokens=settings.experience_output_tokens,
            ),
            full_text_grader=LLMFullTextGrader(
                client,
                settings.grader_model,
                FULL_TEXT_PROMPT_VERSION,
                settings.llm_api(),
                settings.llm_output_mode,
                request_tokens=settings.full_text_request_tokens,
                output_tokens=settings.full_text_output_tokens,
            ),
        )
