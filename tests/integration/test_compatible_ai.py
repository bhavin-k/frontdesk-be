import json
import secrets

import httpx
import pytest
from openai import OpenAI

from recruitment.adapters.ai import CompatibleEmbeddings, LLMGrader, LLMProfiles
from recruitment.domain.contracts import EvidenceInput
from tests.unit.test_ai_configuration import chat_response


@pytest.mark.parametrize("invalid_profile", [False, True])
def test_upload_processing_through_compatible_endpoints(context, cv, invalid_profile):
    original = context.providers
    fake_profiles, fake_grader, fake_embeddings = (
        original.profiles,
        original.grader,
        original.embeddings,
    )
    settings = context.settings
    settings.llm_provider = "llm-gateway"
    settings.llm_base_url = "https://llm.example.invalid/v1"
    settings.profile_model, settings.grader_model = "vendor/profile", "vendor/grader"
    settings.llm_output_mode = "json_object"
    settings.embedding_provider = "vector-service"
    settings.embedding_base_url = "https://vectors.example.invalid/v1"
    settings.embedding_model = "custom-vectors"
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append((request.url.host, request.url.path, body["model"]))
        if request.url.path == "/v1/embeddings":
            vectors = fake_embeddings.embed(body["input"])
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "model": settings.embedding_model,
                    "data": [{"index": i, "embedding": vector} for i, vector in enumerate(vectors)],
                },
            )
        value = json.loads(body["messages"][1]["content"])
        if body["model"] == settings.profile_model:
            result = (
                "{}"
                if invalid_profile
                else fake_profiles.profile(value["cv_reference_text"]).model_dump_json()
            )
        else:
            result = fake_grader.grade(
                value["criterion"],
                [EvidenceInput.model_validate(chunk) for chunk in value["chunks"]],
            ).model_dump_json()
        return httpx.Response(200, json=chat_response(result))

    with (
        OpenAI(
            api_key=secrets.token_urlsafe(32),
            base_url=settings.llm_base_url,
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as llm,
        OpenAI(
            api_key=secrets.token_urlsafe(32),
            base_url=settings.embedding_base_url,
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(respond)),
        ) as embeddings,
    ):
        original.name = settings.llm_provider
        original.profiles = LLMProfiles(
            llm, settings.profile_model, "profile-v1", "chat_completions", "json_object"
        )
        original.grader = LLMGrader(
            llm, settings.grader_model, "matching-v1", "chat_completions", "json_object"
        )
        original.embeddings = CompatibleEmbeddings(embeddings, settings.embedding_model)
        if not invalid_profile:
            changed = context.client.patch(
                f"{context.base}/openings/{context.opening}",
                headers=context.headers,
                json={
                    "title": "Python Developer",
                    "job_description": "Python development via compatible endpoints",
                    "expected_revision": 1,
                },
            )
            assert changed.status_code == 200
            context.drain()
        uploaded = context.upload(cv).json()["upload_id"]
        context.drain()
        if invalid_profile:
            status = context.client.get(
                f"{context.base}/cv-uploads/{uploaded}", headers=context.headers
            ).json()
            assert status["status"] == "needs_review"
            assert status["review_reason"] == "INVALID_MODEL_OUTPUT"
            assert all(request[2] == settings.profile_model for request in requests)
        else:
            result = context.result(uploaded)
            assert result["criteria"][0]["evidence"]
            assert result["score"] is not None
            assert (
                "llm.example.invalid",
                "/v1/chat/completions",
                settings.profile_model,
            ) in requests
            assert (
                "llm.example.invalid",
                "/v1/chat/completions",
                settings.grader_model,
            ) in requests
            assert (
                "vectors.example.invalid",
                "/v1/embeddings",
                settings.embedding_model,
            ) in requests
