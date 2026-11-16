from decimal import Decimal

import pytest

from recruitment.core.config import Settings
from recruitment.domain.contracts import Criterion, EvidenceGrade
from recruitment.domain.matching import calculate_match


def test_partial_required_evidence_requires_review() -> None:
    grade = EvidenceGrade(
        status="partial",
        confidence=0.99,
        evidence_chunk_ids=["chunk-1"],
        quotes=["Some Python experience."],
        reason="The evidence is incomplete.",
    )
    result = calculate_match([Criterion(text="Python")], [grade], Decimal(75))
    assert result.recommendation == "needs_review"


def test_fake_providers_cannot_run_in_production(settings: Settings) -> None:
    values = settings.model_dump()
    values["environment"] = "production"
    with pytest.raises(ValueError, match="Fake providers are forbidden"):
        Settings.model_validate(values)
