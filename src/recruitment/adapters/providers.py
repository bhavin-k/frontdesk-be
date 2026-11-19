from dataclasses import dataclass

from openai import DefaultHttpxClient, OpenAI

from recruitment.adapters.ai import CVParser, JDChecklist, JDMatcher
from recruitment.adapters.conversion import FakeDocumentConverter, GotenbergConverter
from recruitment.adapters.documents import ClamAVScanner
from recruitment.adapters.fakes import (
    FakeChecklistBuilder,
    FakeMatcher,
    FakeOCR,
    FakeProfiles,
    FakeScanner,
)
from recruitment.adapters.mail_runtime import MailboxRuntime
from recruitment.adapters.paddle_ocr import PaddleOCRExtractor
from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError
from recruitment.domain.scoring_contracts import GRADE_PROMPT, RUBRIC_PROMPT
from recruitment.ports.providers import (
    CandidateProfileExtractor,
    ChecklistBuilder,
    DocumentConverter,
    DocumentExtractor,
    JobMatcher,
    MalwareScanner,
)

PARSER_PROMPT = "cv-parser-v3"
MATCHER_PROMPT = GRADE_PROMPT


@dataclass
class Providers:
    scanner: MalwareScanner
    ocr: DocumentExtractor
    profiles: CandidateProfileExtractor
    matcher: JobMatcher
    name: str
    converter: DocumentConverter
    mailboxes: MailboxRuntime | None = None
    client: OpenAI | None = None
    rubrics: ChecklistBuilder | None = None

    def close(self) -> None:
        if self.client is not None:
            self.client.close()

    @classmethod
    def from_settings(cls, settings: Settings) -> "Providers":
        if settings.provider_mode == "fake":
            return cls(
                FakeScanner(),
                FakeOCR(),
                FakeProfiles(),
                FakeMatcher(),
                "fake",
                FakeDocumentConverter(),
                rubrics=FakeChecklistBuilder(),
            )
        llm_key = settings.llm_key()
        if not llm_key:
            raise ProviderError("LLM_NOT_CONFIGURED")
        client = OpenAI(
            api_key=llm_key.get_secret_value(),
            base_url=settings.llm_base_url,
            timeout=settings.llm_timeout_seconds,
            max_retries=0,
            http_client=DefaultHttpxClient(follow_redirects=False),
        )
        return cls(
            ClamAVScanner(settings.clamav_host, settings.clamav_port),
            PaddleOCRExtractor(settings.ocr_models_dir),
            CVParser(
                client,
                settings.profile_model,
                PARSER_PROMPT,
                settings.llm_api(),
                settings.llm_output_mode,
                request_tokens=settings.llm_request_tokens,
                output_tokens=settings.llm_output_tokens,
                chat_disable_thinking=settings.profile_chat_disable_thinking,
            ),
            JDMatcher(
                client,
                settings.grader_model,
                MATCHER_PROMPT,
                settings.llm_api(),
                settings.llm_output_mode,
                request_tokens=settings.llm_request_tokens,
                output_tokens=settings.llm_output_tokens,
                chat_disable_thinking=settings.grader_chat_disable_thinking,
            ),
            settings.llm_provider,
            GotenbergConverter(
                settings.document_converter_url, settings.document_conversion_timeout_seconds
            ),
            client=client,
            rubrics=JDChecklist(
                client,
                settings.grader_model,
                RUBRIC_PROMPT,
                settings.llm_api(),
                settings.llm_output_mode,
                request_tokens=settings.llm_request_tokens,
                output_tokens=settings.llm_output_tokens,
                chat_disable_thinking=settings.grader_chat_disable_thinking,
            ),
        )
