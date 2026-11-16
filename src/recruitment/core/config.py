import base64
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

OPENAI_BASE_URL = "https://api.openai.com/v1"
DEFAULT_FULL_TEXT_REQUEST_TOKENS = 32_768
DEFAULT_FULL_TEXT_OUTPUT_TOKENS = 3072
DEFAULT_EXPERIENCE_REQUEST_TOKENS = 8_000
DEFAULT_EXPERIENCE_OUTPUT_TOKENS = 3072
LLMAPIFormat = Literal["responses", "chat_completions"]
LLMOutputMode = Literal["json_schema", "json_object", "text"]
ScoringStrategy = Literal["rag", "full_text"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RECRUITMENT_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        mode = init_settings().get("environment") or env_settings().get("environment")
        if mode == "test":
            return init_settings, env_settings, file_secret_settings
        return init_settings, env_settings, dotenv_settings, file_secret_settings

    environment: Literal["development", "test", "production"] = "development"
    database_url: SecretStr
    auth_database_url: SecretStr
    worker_database_url: SecretStr
    dispatcher_database_url: SecretStr
    provider_mode: Literal["real", "fake"] = "real"
    scoring_strategy: ScoringStrategy = "full_text"
    cv_token_ceiling: int = Field(default=20_000, ge=1, le=200_000)
    experience_request_tokens: int = Field(
        default=DEFAULT_EXPERIENCE_REQUEST_TOKENS, ge=1024, le=1_000_000
    )
    experience_output_tokens: int = Field(
        default=DEFAULT_EXPERIENCE_OUTPUT_TOKENS, ge=128, le=65_536
    )
    full_text_request_tokens: int = Field(
        default=DEFAULT_FULL_TEXT_REQUEST_TOKENS, ge=1024, le=1_000_000
    )
    full_text_output_tokens: int = Field(default=DEFAULT_FULL_TEXT_OUTPUT_TOKENS, ge=128, le=65_536)
    llm_provider: str = Field(default="openai", min_length=1, max_length=200, pattern=r"\S")
    llm_base_url: str = OPENAI_BASE_URL
    llm_api_key: SecretStr | None = None
    llm_api_format: Literal["auto", "responses", "chat_completions"] = "auto"
    llm_output_mode: LLMOutputMode = "json_schema"
    llm_timeout_seconds: float = Field(default=45, gt=0, le=120)
    # Compatibility with existing installations; never use this at a custom endpoint.
    openai_api_key: SecretStr | None = None
    profile_model: str = Field(default="gpt-6-luna", min_length=1, max_length=200, pattern=r"\S")
    grader_model: str = Field(default="gpt-6.1-terra", min_length=1, max_length=200, pattern=r"\S")
    embedding_model: str = Field(
        default="text-embedding-3-large", min_length=1, max_length=200, pattern=r"\S"
    )
    embedding_dimensions: Literal[1536] = 1536
    embedding_provider: str = Field(default="openai", min_length=1, max_length=200, pattern=r"\S")
    embedding_base_url: str = OPENAI_BASE_URL
    embedding_api_key: SecretStr | None = None
    embedding_send_dimensions: bool = True
    embedding_revision: str = Field(default="1", min_length=1, max_length=200, pattern=r"\S")
    embedding_batch_size: int = Field(default=128, ge=1, le=128)
    embedding_max_batch_tokens: int = Field(default=64000, ge=500, le=64000)
    embedding_timeout_seconds: float = Field(default=30, gt=0, le=120)
    embedding_max_parallel_calls: int = Field(default=4, ge=1, le=8)
    chunk_size_tokens: int = Field(default=400, ge=300, le=500)
    chunk_overlap_tokens: int = Field(default=64, ge=50, le=80)
    retrieval_mode: Literal["hybrid", "semantic"] = "hybrid"
    retrieval_candidates_per_method: int = Field(default=12, ge=1, le=100)
    retrieval_top_k: int = Field(default=5, ge=1, le=20)
    retrieval_neighbor_window: int = Field(default=1, ge=0, le=3)
    retrieval_context_tokens: int = Field(default=3000, ge=500, le=16000)
    retrieval_rrf_k: int = Field(default=60, ge=1, le=1000)
    mailbox_provider_mode: Literal["real", "fake"] = "fake"
    enabled_mailbox_methods: str = "gmail_oauth,microsoft_oauth"
    imap_poll_seconds: int = Field(default=60, ge=30, le=900)
    imap_timeout_seconds: int = Field(default=10, ge=1, le=30)
    public_app_url: str = "http://localhost:3100"
    public_webhook_url: str | None = None
    credential_keys: SecretStr | None = None  # JSON {key_id: base64-encoded 32-byte key}
    credential_active_key: str = "1"
    gmail_client_id: str | None = None
    gmail_client_secret: SecretStr | None = None
    gmail_pubsub_topic: str | None = None
    gmail_push_audience: str | None = None
    gmail_push_service_account: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: SecretStr | None = None
    microsoft_authority: str = "https://login.microsoftonline.com/common"
    document_converter_url: str = "http://127.0.0.1:8300"
    document_conversion_timeout_seconds: int = Field(default=60, ge=1, le=120)
    hnsw_ef_search: int = 40
    aws_region: str = "ap-south-1"
    clamav_host: str = "127.0.0.1"
    clamav_port: int = 3310
    session_seconds: int = 3600
    auto_delete_enabled: bool = False
    session_limit: int = 10
    login_attempt_limit: int = 10
    lease_seconds: int = 300
    max_job_attempts: int = 3
    worker_poll_seconds: float = 1.0

    @field_validator("embedding_dimensions", mode="before")
    @classmethod
    def parse_embedding_dimensions(cls, value: object) -> object:
        return 1536 if isinstance(value, str) and value.strip() == "1536" else value

    @field_validator("llm_provider", "embedding_provider")
    @classmethod
    def validate_ai_provider(cls, value: str) -> str:
        value = value.strip()
        if not value or value.casefold() == "fake":
            raise ValueError("Use a provider name; select local fakes with provider_mode instead.")
        return value

    @field_validator("llm_api_key", "openai_api_key", "embedding_api_key", mode="before")
    @classmethod
    def empty_ai_key_is_unset(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if isinstance(raw, str):
            return raw.strip() or None
        return value

    @field_validator("llm_base_url", "embedding_base_url")
    @classmethod
    def validate_ai_endpoint(cls, value: str) -> str:
        endpoint = urlsplit(value)
        if (
            endpoint.scheme != "https"
            or not endpoint.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("AI endpoints require an HTTPS URL without credentials or query data.")
        return value.rstrip("/")

    @field_validator("public_webhook_url", mode="before")
    @classmethod
    def empty_webhook_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def production_guards(self) -> "Settings":
        if self.full_text_output_tokens >= self.full_text_request_tokens:
            raise ValueError("Full-text output tokens must be less than the total request budget.")
        if self.experience_output_tokens >= self.experience_request_tokens:
            raise ValueError("Experience output tokens must be less than the total request budget.")
        methods = [
            value.strip() for value in self.enabled_mailbox_methods.split(",") if value.strip()
        ]
        if len(methods) != len(set(methods)) or set(methods) - {
            "imap",
            "gmail_oauth",
            "microsoft_oauth",
        }:
            raise ValueError("Choose distinct mailbox methods: imap, gmail_oauth, microsoft_oauth.")
        self.enabled_mailbox_methods = ",".join(methods)
        if self.environment == "production" and "imap" in methods:
            raise ValueError("IMAP App Password connections are for development and testing only.")
        if self.environment == "production" and (
            self.provider_mode == "fake" or self.mailbox_provider_mode == "fake"
        ):
            raise ValueError("Fake providers are forbidden in production.")
        if self.llm_api() == "responses" and self.llm_output_mode != "json_schema":
            raise ValueError(
                "Responses requires json_schema output; use chat_completions otherwise"
            )
        if self.retrieval_top_k > self.retrieval_candidates_per_method:
            raise ValueError("Retrieval top K cannot exceed the candidates per method.")
        for origin in (self.public_app_url, self.public_webhook_url):
            if origin is None:
                continue
            parsed = urlsplit(origin)
            local = parsed.hostname in {"localhost", "127.0.0.1"}
            if (
                not parsed.hostname
                or parsed.username
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme != "https"
                    and not (parsed.scheme == "http" and local and self.environment != "production")
                )
            ):
                raise ValueError("Public URLs require HTTPS (HTTP loopback in development).")
        converter = urlsplit(self.document_converter_url)
        if (
            converter.scheme not in {"http", "https"}
            or not converter.hostname
            or (converter.username or converter.query or converter.fragment)
        ):
            raise ValueError("Document converter requires a private HTTP(S) endpoint.")
        if self.credential_keys and self.credential_keys.get_secret_value():
            try:
                keys = json.loads(self.credential_keys.get_secret_value())
                if not isinstance(keys, dict) or self.credential_active_key not in keys:
                    raise ValueError
                if any(
                    not isinstance(key, str)
                    or not key
                    or len(base64.b64decode(value, validate=True)) != 32
                    for key, value in keys.items()
                ):
                    raise ValueError
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "Configure a valid 256-bit credential key ring and active key."
                ) from exc
        return self

    def mailbox_method_enabled(self, method: str) -> bool:
        return method in self.enabled_mailbox_methods.split(",") and not (
            method == "imap" and self.environment == "production"
        )

    def llm_api(self) -> LLMAPIFormat:
        if self.llm_api_format != "auto":
            return self.llm_api_format
        return "responses" if self.llm_base_url == OPENAI_BASE_URL else "chat_completions"

    def llm_key(self) -> SecretStr | None:
        if self.llm_api_key and self.llm_api_key.get_secret_value():
            return self.llm_api_key
        if self.llm_base_url.rstrip("/") == OPENAI_BASE_URL:
            return self.openai_api_key
        return None

    def embedding_key(self) -> SecretStr | None:
        if self.embedding_api_key and self.embedding_api_key.get_secret_value():
            return self.embedding_api_key
        if self.embedding_base_url.rstrip("/") == OPENAI_BASE_URL:
            if self.llm_base_url.rstrip("/") == OPENAI_BASE_URL:
                return self.llm_key()
            return self.openai_api_key
        return None

    def processing_snapshot(self) -> dict[str, str | int | float]:
        fields = (
            "llm_provider",
            "llm_base_url",
            "llm_output_mode",
            "llm_timeout_seconds",
            "embedding_provider",
            "embedding_base_url",
            "embedding_model",
            "embedding_dimensions",
            "embedding_revision",
            "embedding_send_dimensions",
            "embedding_batch_size",
            "embedding_max_batch_tokens",
            "embedding_timeout_seconds",
            "embedding_max_parallel_calls",
            "profile_model",
            "grader_model",
            "chunk_size_tokens",
            "chunk_overlap_tokens",
            "retrieval_mode",
            "retrieval_candidates_per_method",
            "retrieval_top_k",
            "retrieval_neighbor_window",
            "retrieval_context_tokens",
            "retrieval_rrf_k",
        )
        snapshot = {field: getattr(self, field) for field in fields}
        snapshot["llm_api_format"] = self.llm_api()
        return snapshot
