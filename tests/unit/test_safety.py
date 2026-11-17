import pytest

from recruitment.core.config import Settings


def test_fake_providers_cannot_run_in_production(settings: Settings) -> None:
    values = settings.model_dump()
    values["environment"] = "production"
    with pytest.raises(ValueError, match="Fake providers are forbidden"):
        Settings.model_validate(values)
