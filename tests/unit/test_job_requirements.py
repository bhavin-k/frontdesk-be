from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from recruitment.adapters.ai import LLMJobCriteria
from recruitment.core.errors import AppError, ProviderError
from recruitment.domain.openings import (
    GeneratedRequirements,
    OpeningCreate,
    RequirementSource,
    checked_requirements,
)


def generated(**changes):
    return GeneratedRequirements.model_validate(
        {
            "criteria": [
                {
                    "text": "Python API development",
                    "required": True,
                    "strict": True,
                    "source_id": "description",
                    "quote": "Must build Python APIs.",
                    **changes,
                }
            ]
        }
    )


def test_requirements_have_verified_sources_and_backend_owned_weights():
    sources = [RequirementSource(id="description", text="Must build Python APIs.")]
    result = checked_requirements(generated(), sources)
    assert result[0][0].weight == 1 and result[0][0].strict
    assert result[0][1]["quote"] == sources[0].text
    loose = checked_requirements(generated(quote="Python APIs"), sources)
    assert not loose[0][0].strict
    with pytest.raises(ValidationError):
        generated(weight=100)
    with pytest.raises(ValidationError):
        OpeningCreate(
            title="Developer", job_description="Python development", shortlist_threshold=1
        )


@pytest.mark.parametrize(
    "changes", [{"quote": "Unsupported text"}, {"source_id": "another-tenant"}]
)
def test_unverified_job_evidence_is_rejected(changes):
    with pytest.raises(ProviderError, match="INVALID_JOB_EVIDENCE"):
        checked_requirements(
            generated(**changes),
            [RequirementSource(id="description", text="Must build Python APIs.")],
        )


def test_protected_characteristics_cannot_become_job_criteria():
    with pytest.raises(AppError, match="job-related"):
        checked_requirements(
            generated(text="Male candidates only"),
            [RequirementSource(id="description", text="Must build Python APIs.")],
        )


def test_job_extractor_uses_the_shared_llm_adapter_without_model_fallback():
    client = MagicMock()
    client.responses.parse.return_value = SimpleNamespace(output_parsed=generated())
    adapter = LLMJobCriteria(client, "configured-profile-model", "job-criteria-v1")
    result = adapter.requirements(
        [RequirementSource(id="description", text="Must build Python APIs.")]
    )
    assert result.criteria[0].source_id == "description"
    request = client.responses.parse.call_args.kwargs
    assert request["model"] == "configured-profile-model" and request["store"] is False
    assert request["text_format"] is GeneratedRequirements
    assert "Must build Python APIs" not in request["instructions"]
    assert "Must build Python APIs" in request["input"]
