import pytest
from pydantic import SecretStr, ValidationError

from recruitment.adapters.providers import Providers
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
