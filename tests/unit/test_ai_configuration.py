import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai.types import CompletionUsage
from pydantic import SecretStr, ValidationError

from recruitment.adapters.fakes import FakeProfiles
from recruitment.adapters.providers import PARSER_PROMPT, Providers
from recruitment.core.config import OPENAI_BASE_URL, Settings
from recruitment.core.errors import ProviderError


def make_settings(**values):
    defaults = {
        "database_url": "postgresql://fake",
        "auth_database_url": "postgresql://fake",
        "worker_database_url": "postgresql://fake",
        "dispatcher_database_url": "postgresql://fake",
        "environment": "test",
        "provider_mode": "real",
    }
    return Settings(_env_file=None, **(defaults | values))


def test_defaults_keep_the_configured_models_and_official_key():
    key = SecretStr("test-key")
    settings = make_settings(openai_api_key=key)
    assert settings.llm_api() == "responses"
    assert settings.llm_base_url == OPENAI_BASE_URL
    assert settings.llm_key() == key
    providers = Providers.from_settings(settings)
    try:
        assert providers.profiles.model == "gpt-6-luna"
        assert providers.matcher.model == "gpt-6.1-terra"
        assert providers.client is not None and not providers.client._client.follow_redirects
    finally:
        providers.close()


def test_compatible_endpoint_requires_its_own_key():
    args = {
        "llm_base_url": "https://gateway.example.invalid/v1",
        "openai_api_key": "official-key",
        "llm_output_mode": "json_object",
    }
    settings = make_settings(**args)
    assert settings.llm_api() == "chat_completions" and settings.llm_key() is None
    with pytest.raises(ProviderError, match="LLM_NOT_CONFIGURED"):
        Providers.from_settings(settings)
    assert make_settings(**args, llm_api_key="gateway-key").llm_key() == SecretStr("gateway-key")


@pytest.mark.parametrize(
    "disable_thinking,extra_body",
    [
        (None, None),
        ("true", {"thinking": {"type": "disabled"}}),
    ],
)
def test_parser_reads_chat_option_and_models_from_env(
    monkeypatch, caplog, disable_thinking, extra_body
):
    monkeypatch.setenv("RECRUITMENT_PROFILE_MODEL", "configured-parser")
    monkeypatch.setenv("RECRUITMENT_GRADER_MODEL", "configured-matcher")
    if disable_thinking is None:
        monkeypatch.delenv("RECRUITMENT_PROFILE_CHAT_DISABLE_THINKING", raising=False)
    else:
        monkeypatch.setenv("RECRUITMENT_PROFILE_CHAT_DISABLE_THINKING", disable_thinking)
    profile = FakeProfiles().profile("Fictional candidate\nPython engineering")
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        usage=CompletionUsage(prompt_tokens=80, completion_tokens=20, total_tokens=100),
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(refusal=None, content=profile.model_dump_json()),
            )
        ],
    )
    monkeypatch.setattr("recruitment.adapters.providers.OpenAI", lambda **kwargs: client)
    providers = Providers.from_settings(
        make_settings(
            llm_provider="compatible",
            llm_base_url="https://gateway.example.invalid/v1",
            llm_api_key="test-key",
            llm_output_mode="text",
        )
    )
    try:
        with caplog.at_level(logging.INFO, logger="recruitment.adapters.ai"):
            assert providers.profiles.profile("Fictional CV") == profile
        request = client.chat.completions.create.call_args.kwargs
        assert request["model"] == "configured-parser"
        assert request["extra_body"] == extra_body
        assert request["max_completion_tokens"] == 3072
        assert providers.matcher.model == "configured-matcher"
        assert providers.matcher.chat_extra_body is None
        usage = f"prompt={PARSER_PROMPT} input_tokens=80 output_tokens=20 total_tokens=100"
        assert usage in caplog.text
    finally:
        providers.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.invalid",
        "https://a:b@example.invalid",
        "https://example.invalid/?key=value",
    ],
)
def test_rejects_unsafe_endpoints(endpoint):
    with pytest.raises(ValidationError):
        make_settings(llm_base_url=endpoint)


def test_rejects_bad_request_budget():
    with pytest.raises(ValidationError):
        make_settings(llm_request_tokens=1500, llm_output_tokens=2000)
