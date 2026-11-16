import base64
import json
import math
import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr, ValidationError

from recruitment.adapters.ai import CompatibleEmbeddings, validate_vectors
from recruitment.adapters.vault import AESCredentialVault
from recruitment.core.config import Settings
from recruitment.core.errors import ProviderError
from recruitment.domain.rag import (
    RankedChunk,
    embedding_batches,
    embedding_fingerprint,
    evidence_selection,
    keyword_rank,
    technical_terms,
    token_count,
)


def test_configuration_is_validated_and_secret_free(settings: Settings) -> None:
    settings.embedding_api_key = SecretStr(secrets.token_urlsafe(32))
    snapshot = settings.processing_snapshot()
    assert snapshot["chunk_size_tokens"] == 400
    assert snapshot["embedding_dimensions"] == 1536
    assert "api_key" not in json.dumps(snapshot)
    for changes in (
        {"embedding_dimensions": 1024},
        {"chunk_size_tokens": 600},
        {"chunk_overlap_tokens": 20},
        {"embedding_max_parallel_calls": 0},
        {"retrieval_top_k": 13},
        {"embedding_base_url": "http://example.org"},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**settings.model_dump(), **changes})


def test_custom_endpoint_never_inherits_openai_key(settings: Settings) -> None:
    settings.openai_api_key = SecretStr(secrets.token_urlsafe(32))
    assert settings.embedding_key() == settings.openai_api_key
    settings.embedding_base_url = "https://embeddings.example.org/v1"
    assert settings.embedding_key() is None
    settings.embedding_api_key = SecretStr(secrets.token_urlsafe(32))
    assert settings.embedding_key() == settings.embedding_api_key


def test_fingerprint_covers_provider_endpoint_model_dimensions_and_revision() -> None:
    initial = ["openai", "https://api.openai.com/v1", "text-embedding-3-large", 1536, "1"]
    original = embedding_fingerprint(*initial)
    for index, value in enumerate(
        ["custom", "https://other.example.org/v1", "different", 3072, "2"]
    ):
        changed = list(initial)
        changed[index] = value
        assert embedding_fingerprint(*changed) != original
    assert (
        embedding_fingerprint("openai", "https://api.openai.com/v1/", initial[2], 1536, "1")
        == original
    )


def test_batches_keep_order_and_respect_both_limits() -> None:
    texts = [f"Python database evidence {i}. " * 80 for i in range(9)]
    batches = embedding_batches(texts, 3, 1200)
    assert [text for batch in batches for text in batch] == texts
    assert all(len(batch) <= 3 and sum(map(token_count, batch)) <= 1200 for batch in batches)
    with pytest.raises(ValueError):
        embedding_batches(["Python " * 1000], 2, 500)


def test_vectors_are_normalized_and_invalid_responses_fail() -> None:
    assert math.isclose(math.hypot(*validate_vectors([[3.0] * 1536], 1)[0]), 1)
    for bad in ([], [[0.0] * 1536], [[float("nan")] * 1536], [[1.0] * 3]):
        with pytest.raises(ProviderError):
            validate_vectors(bad, 1)
    client = MagicMock()
    client.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=1, embedding=[1.0] * 1536)]
    )
    with pytest.raises(ProviderError, match="INVALID_EMBEDDING_ORDER"):
        CompatibleEmbeddings(client, "text-embedding-3-large").embed(["Python"])


def test_lexical_retrieval_keeps_technical_identifiers_distinct() -> None:
    chunks = [RankedChunk("one", 0, "C# and Java"), RankedChunk("two", 1, "C++ .NET AZ-104")]
    assert {"c++", "c#", ".net", "az-104"} <= technical_terms("C++ C# .NET AZ-104")
    assert keyword_rank(chunks, "C++ .NET", 12) == ["two"]
    assert keyword_rank(chunks, "C#", 12) == ["one"]


def test_ranking_reserves_primary_then_neighbors_and_preserves_ids_and_budget() -> None:
    chunks = [RankedChunk(str(i), i, f"Evidence {i}. " * 100) for i in range(5)]
    result = evidence_selection(
        chunks, ["2", "4"], ["2"], top_k=1, neighbor_window=1, budget=1500, rrf_k=60
    )
    assert [item.chunk_id for item in result] == ["1", "2", "3"]
    for item in result:
        assert item.text == chunks[int(item.chunk_id)].text
    assert (
        token_count(json.dumps([item.model_dump() for item in result], ensure_ascii=False)) <= 1500
    )
    restricted = evidence_selection(
        chunks, ["2"], [], top_k=1, neighbor_window=1, budget=500, rrf_k=60
    )
    assert [item.chunk_id for item in restricted] == ["2"]


def test_vault_rotation_and_tenant_binding() -> None:
    keys = {name: base64.b64encode(secrets.token_bytes(32)).decode() for name in ("old", "new")}
    old = AESCredentialVault(keys, "old")
    sealed = old.seal({"refresh_token": secrets.token_urlsafe(32)}, "business-A:mailbox")
    rotated = AESCredentialVault(keys, "new")
    assert rotated.open(sealed, "business-A:mailbox")
    assert rotated.seal({}, "business-A:mailbox").key_id == "new"
    with pytest.raises(ProviderError, match="CREDENTIAL_DECRYPTION_FAILED"):
        rotated.open(sealed, "business-B:mailbox")


def test_environment_overrides_dotenv_and_invalid_configured_values_fail(
    settings, tmp_path, monkeypatch
):
    path = tmp_path / ".env"
    path.write_text("RECRUITMENT_CHUNK_SIZE_TOKENS=350\n", encoding="utf-8")
    monkeypatch.setenv("RECRUITMENT_CHUNK_SIZE_TOKENS", "450")
    database_fields = {
        name: getattr(settings, name)
        for name in (
            "database_url",
            "auth_database_url",
            "worker_database_url",
            "dispatcher_database_url",
        )
    }
    configured = Settings(
        _env_file=path, environment="development", provider_mode="fake", **database_fields
    )
    assert configured.chunk_size_tokens == 450
    monkeypatch.setenv("RECRUITMENT_CHUNK_SIZE_TOKENS", "900")
    with pytest.raises(ValidationError):
        Settings(_env_file=path, environment="development", provider_mode="fake", **database_fields)
    assert keyword_rank([RankedChunk("one", 0, "Python. C++. .NET.")], "Python C++ .NET", 12) == [
        "one"
    ]


def test_test_mode_does_not_read_a_developers_dotenv(settings, tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("RECRUITMENT_CHUNK_SIZE_TOKENS=999\n", encoding="utf-8")
    monkeypatch.delenv("RECRUITMENT_CHUNK_SIZE_TOKENS", raising=False)
    database_fields = {
        name: getattr(settings, name)
        for name in (
            "database_url",
            "auth_database_url",
            "worker_database_url",
            "dispatcher_database_url",
        )
    }
    configured = Settings(
        _env_file=path,
        environment="test",
        provider_mode="fake",
        **database_fields,
    )
    assert configured.chunk_size_tokens == 400
    with pytest.raises(ValidationError):
        Settings(_env_file=path, environment="development", provider_mode="fake", **database_fields)
