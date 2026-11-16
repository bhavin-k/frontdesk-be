import json
import math
import secrets
from unittest.mock import MagicMock

import httpx
import pytest
from openai import OpenAI
from pydantic import SecretStr, ValidationError

from recruitment.adapters.ai import CompatibleEmbeddings, LLMGrader, LLMProfiles, StructuredAI
from recruitment.adapters.providers import Providers
from recruitment.core.config import OPENAI_BASE_URL, Settings
from recruitment.core.errors import ProviderError
from recruitment.domain.contracts import EvidenceInput
from tests.unit.test_provider_contracts import valid_profile


def key() -> SecretStr:
    return SecretStr(secrets.token_urlsafe(32))


def configured(settings: Settings, **changes) -> Settings:
    return Settings.model_validate({**settings.model_dump(), **changes})


def chat_response(content, *, finish="stop", refusal=None):
    return {
        "id": "chat-test",
        "object": "chat.completion",
        "created": 0,
        "model": "gateway-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
            }
        ],
    }


def test_generic_keys_take_precedence_and_legacy_openai_keeps_working(settings):
    old, new, embeddings = key(), key(), key()
    current = configured(settings, openai_api_key=old)
    assert current.llm_key() == old and current.embedding_key() == old
    current = configured(current, llm_api_key=new)
    assert current.llm_key() == new and current.embedding_key() == new
    current = configured(current, embedding_api_key=embeddings)
    assert current.embedding_key() == embeddings
    assert current.profile_model == "gpt-6-luna"
    assert current.grader_model == "gpt-6.1-terra"
    assert current.embedding_model == "text-embedding-3-large"


def test_custom_services_require_explicit_keys_without_cross_provider_fallback(settings):
    old, llm = key(), key()
    custom = configured(
        settings,
        openai_api_key=old,
        llm_provider="gateway",
        llm_base_url="https://llm.example.invalid/v1/",
        embedding_base_url="https://vectors.example.invalid/v1",
    )
    assert custom.llm_key() is None and custom.embedding_key() is None
    custom = configured(custom, llm_api_key=llm)
    assert custom.llm_key() == llm and custom.embedding_key() is None
    # The gateway key must not be forwarded to the official OpenAI embeddings service.
    official = configured(custom, embedding_base_url=OPENAI_BASE_URL, openai_api_key=None)
    assert official.embedding_key() is None
    assert configured(official, openai_api_key=old).embedding_key() == old


@pytest.mark.parametrize("field", ["llm_base_url", "embedding_base_url"])
@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.invalid/v1",
        "https://user:password@api.example.invalid/v1",
        "https://api.example.invalid/v1?api_key=private-value",
        "https://api.example.invalid/v1#private-value",
    ],
)
def test_ai_urls_reject_unsafe_values_without_echoing_them(settings, field, url):
    with pytest.raises(ValidationError) as caught:
        configured(settings, **{field: url})
    assert url not in str(caught.value)


def test_formats_are_explicit_and_auto_preserves_the_existing_api(settings):
    assert settings.llm_api() == "responses"
    custom = configured(settings, llm_base_url="https://llm.example.invalid/v1/")
    assert custom.llm_base_url.endswith("/v1") and custom.llm_api() == "chat_completions"
    assert configured(custom, llm_api_format="responses").llm_api() == "responses"
    for changes in (
        {"llm_api_format": "unknown"},
        {"llm_output_mode": "unknown"},
        {"llm_output_mode": "json_object"},
        {"llm_timeout_seconds": 0},
        {"llm_provider": "fake"},
        {"embedding_provider": "fake"},
    ):
        with pytest.raises(ValidationError):
            configured(settings, **changes)


def test_api_and_worker_read_generic_env_keys_and_model_ids(settings, monkeypatch, tmp_path):
    llm, embeddings = key(), key()
    path = tmp_path / ".env"
    path.write_text(
        "RECRUITMENT_LLM_PROVIDER=compatible-gateway\n"
        "RECRUITMENT_LLM_BASE_URL=https://llm.example.invalid/v1\n"
        "RECRUITMENT_PROFILE_MODEL=profile-from-dotenv\n"
        "RECRUITMENT_GRADER_MODEL=grader-from-dotenv\n"
        "RECRUITMENT_EMBEDDING_MODEL=embedding-from-dotenv\n"
        "RECRUITMENT_EMBEDDING_DIMENSIONS=1536\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RECRUITMENT_LLM_API_KEY", llm.get_secret_value())
    monkeypatch.setenv("RECRUITMENT_EMBEDDING_API_KEY", embeddings.get_secret_value())
    monkeypatch.setenv("RECRUITMENT_PROFILE_MODEL", "profile-from-environment")
    databases = {
        name: getattr(settings, name)
        for name in (
            "database_url",
            "auth_database_url",
            "worker_database_url",
            "dispatcher_database_url",
        )
    }
    for _ in range(2):
        loaded = Settings(_env_file=path, environment="development", **databases)
        assert loaded.llm_key() == llm and loaded.embedding_key() == embeddings
        assert loaded.profile_model == "profile-from-environment"
        assert loaded.grader_model == "grader-from-dotenv"
        assert loaded.embedding_model == "embedding-from-dotenv"
        assert loaded.embedding_dimensions == 1536
        assert loaded.llm_api() == "chat_completions"


@pytest.mark.parametrize("value", ["1536", "3072", "invalid"])
def test_embedding_dimensions_from_environment_keep_the_schema_pin(settings, monkeypatch, value):
    monkeypatch.setenv("RECRUITMENT_EMBEDDING_DIMENSIONS", value)
    databases = {k: v for k, v in settings.model_dump().items() if "database_url" in k}
    if value == "1536":
        loaded = Settings(_env_file=None, environment="test", **databases)
        assert loaded.embedding_dimensions == 1536
    else:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, environment="test", **databases)


def test_processing_snapshot_is_secret_free_and_records_routing(settings):
    llm, embeddings = key(), key()
    custom = configured(
        settings,
        llm_api_key=llm,
        embedding_api_key=embeddings,
        llm_base_url="https://llm.example.invalid/v1",
        llm_provider="gateway",
        llm_output_mode="json_object",
        embedding_send_dimensions=False,
    )
    snapshot = custom.processing_snapshot()
    serialized = json.dumps(snapshot)
    assert snapshot["llm_api_format"] == "chat_completions"
    assert snapshot["llm_output_mode"] == "json_object"
    assert snapshot["llm_provider"] == "gateway"
    assert snapshot["embedding_send_dimensions"] is False
    assert "api_key" not in serialized
    assert llm.get_secret_value() not in serialized
    assert embeddings.get_secret_value() not in serialized


def test_factory_routes_separate_clients_and_preserves_fake_mode(settings, monkeypatch):
    llm, embeddings = key(), key()
    llm_client, embedding_client = MagicMock(), MagicMock()
    factory = MagicMock(side_effect=[llm_client, embedding_client])
    monkeypatch.setattr("recruitment.adapters.providers.OpenAI", factory)
    monkeypatch.setattr("recruitment.adapters.providers.TextractOCR", MagicMock())
    custom = configured(
        settings,
        provider_mode="real",
        llm_provider="compatible-gateway",
        llm_base_url="https://llm.example.invalid/v1",
        llm_api_key=llm,
        profile_model="vendor/profile-model",
        grader_model="vendor/grader-model",
        embedding_provider="vector-service",
        embedding_base_url="https://vectors.example.invalid/v1",
        embedding_api_key=embeddings,
        embedding_model="vector-model",
        embedding_send_dimensions=False,
    )
    providers = Providers.from_settings(custom)
    calls = [call.kwargs for call in factory.call_args_list]
    try:
        assert calls[0]["base_url"] == custom.llm_base_url
        assert calls[0]["api_key"] == llm.get_secret_value()
        assert calls[0]["timeout"] == 45
        assert calls[1]["base_url"] == custom.embedding_base_url
        assert calls[1]["api_key"] == embeddings.get_secret_value()
        assert calls[1]["timeout"] == 30
        assert all(call["max_retries"] == 0 for call in calls)
        assert all(not call["http_client"].follow_redirects for call in calls)
        assert providers.profiles.model == "vendor/profile-model"
        assert providers.grader.model == "vendor/grader-model"
        assert providers.embeddings.model == "vector-model"
        assert providers.profiles.api_format == "chat_completions"
        assert providers.name == "compatible-gateway"
        assert providers.fingerprint != Providers.from_settings(settings).fingerprint
        providers.close()
        llm_client.close.assert_called_once()
        embedding_client.close.assert_called_once()
        factory.reset_mock()
        fake = Providers.from_settings(configured(custom, provider_mode="fake"))
        assert fake.name == "fake" and fake.profiles.model != custom.profile_model
        factory.assert_not_called()
    finally:
        for call in calls:
            call["http_client"].close()


@pytest.mark.parametrize("mode", ["json_schema", "json_object", "text"])
def test_compatible_chat_uses_arbitrary_model_and_validates_profile(mode):
    requests = []
    secret = key()

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=chat_response(valid_profile().model_dump_json()))

    with OpenAI(
        api_key=secret.get_secret_value(),
        base_url="https://llm.example.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        profile = LLMProfiles(client, "vendor/custom-model", "profile-v1", "chat_completions", mode)
        assert profile.profile("CV reference: Python APIs") == valid_profile()
    request = requests[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://llm.example.invalid/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {secret.get_secret_value()}"
    assert body["model"] == "vendor/custom-model" and body["store"] is False
    assert "CV reference: Python APIs" not in body["messages"][0]["content"]
    assert "CV reference: Python APIs" in body["messages"][1]["content"]
    assert "JSON" in body["messages"][0]["content"]
    if mode == "text":
        assert "response_format" not in body
    else:
        assert body["response_format"]["type"] == mode


def test_chat_profile_uses_zero_temperature():
    requests = []
    expected = valid_profile()

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=chat_response(expected.model_dump_json()))

    with OpenAI(
        api_key=key().get_secret_value(),
        base_url="https://llm.example.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        for mode in ("json_schema", "json_object"):
            profile = LLMProfiles(client, "custom", "profile-v1", "chat_completions", mode)
            assert profile.profile("Fictional CV about Python APIs") == expected
        StructuredAI(client, "custom", "profile-v1", "chat_completions").parse(
            "Fictional CV about Python APIs", type(expected)
        )

    assert requests[0]["temperature"] == requests[1]["temperature"] == 0
    assert "temperature" not in requests[2]


def test_compatible_grading_keeps_criterion_and_evidence_identifiers():
    client = MagicMock()
    result = {
        "status": "met",
        "confidence": 0.99,
        "evidence_chunk_ids": ["chunk-1"],
        "quotes": ["Python APIs"],
        "reason": "The CV describes Python API development.",
    }
    client.chat.completions.create.return_value.choices = [
        MagicMock(finish_reason="stop", message=MagicMock(content=json.dumps(result), refusal=None))
    ]
    grade = LLMGrader(client, "custom-grader", "matching-v1", "chat_completions", "text").grade(
        "Python development", [EvidenceInput(chunk_id="chunk-1", text="Python APIs")]
    )
    assert grade.evidence_chunk_ids == ["chunk-1"]
    request = client.chat.completions.create.call_args.kwargs
    payload = json.loads(request["messages"][1]["content"])
    assert payload["criterion"] == "Python development"
    assert payload["chunks"] == [{"chunk_id": "chunk-1", "text": "Python APIs"}]


@pytest.mark.parametrize("content", ["invalid JSON", "{}", '{"unexpected":true}'])
def test_invalid_chat_output_retries_once_without_returning_a_profile(content):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=chat_response(content))

    with (
        OpenAI(
            api_key=key().get_secret_value(),
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client,
        pytest.raises(ProviderError, match="INVALID_MODEL_OUTPUT"),
    ):
        LLMProfiles(client, "custom", "profile-v1", "chat_completions", "text").profile("CV")
    assert len(requests) == 2
    assert "previous response failed" in requests[1]["messages"][0]["content"]


@pytest.mark.parametrize("mode", ["json_schema", "text"])
@pytest.mark.parametrize(
    "finish,refusal", [("length", None), ("stop", "Cannot process"), ("content_filter", None)]
)
def test_incomplete_or_refused_chat_output_is_unavailable(mode, finish, refusal):
    with (
        OpenAI(
            api_key=key().get_secret_value(),
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        200,
                        json=chat_response(
                            valid_profile().model_dump_json(), finish=finish, refusal=refusal
                        ),
                    )
                )
            ),
        ) as client,
        pytest.raises(ProviderError, match="MODEL_OUTPUT_UNAVAILABLE"),
    ):
        LLMProfiles(client, "custom", "profile-v1", "chat_completions", mode).profile("CV")


@pytest.mark.parametrize(
    "status,retryable", [(400, False), (401, False), (408, True), (429, True), (503, True)]
)
def test_compatible_provider_http_failures_are_safe_and_bounded(status, retryable):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": "PRIVATE_CV_AND_KEY"}})

    with (
        OpenAI(
            api_key=key().get_secret_value(),
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as client,
        pytest.raises(ProviderError) as caught,
    ):
        LLMProfiles(client, "custom", "profile-v1", "chat_completions", "text").profile("CV")
    assert caught.value.retryable is retryable and len(requests) == 1
    assert "PRIVATE" not in str(caught.value)


def test_embeddings_can_omit_dimensions_but_must_still_return_1536():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [float(j == i) for j in range(1536)]} for i in (1, 0)
                ],
                "model": "custom-embedding",
                "object": "list",
            },
        )

    with OpenAI(
        api_key=key().get_secret_value(),
        base_url="https://vectors.example.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    ) as client:
        adapter = CompatibleEmbeddings(client, "custom-embedding", send_dimensions=False)
        result = adapter.embed(["First chunk", "Second chunk"])
    assert len(result) == 2 and all(math.isclose(math.hypot(*vector), 1) for vector in result)
    assert result[0][0] == result[1][1] == 1
    assert requests[0]["model"] == "custom-embedding" and "dimensions" not in requests[0]
    assert requests[0]["input"] == ["First chunk", "Second chunk"]


def test_missing_llm_credentials_fail_before_initializing_any_clients(settings, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr("recruitment.adapters.providers.OpenAI", factory)
    with pytest.raises(ProviderError, match="LLM_NOT_CONFIGURED"):
        Providers.from_settings(configured(settings, provider_mode="real"))
    factory.assert_not_called()


def test_custom_embedding_credentials_are_required_before_clients_are_initialized(
    settings, monkeypatch
):
    factory = MagicMock()
    monkeypatch.setattr("recruitment.adapters.providers.OpenAI", factory)
    with pytest.raises(ProviderError, match="EMBEDDING_NOT_CONFIGURED"):
        Providers.from_settings(
            configured(
                settings,
                provider_mode="real",
                llm_api_key=key(),
                embedding_base_url="https://vectors.example.invalid/v1",
            )
        )
    factory.assert_not_called()
