import pytest
from pydantic import ValidationError

from ai_task_assistant.core.config import Settings


@pytest.mark.parametrize("provider", ["openai", "gemini"])
def test_valid_ai_provider_can_be_loaded(provider: str) -> None:
    settings = Settings(_env_file=None, ai_provider=provider)

    assert settings.ai_provider == provider


def test_invalid_ai_provider_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ai_provider"):
        Settings(_env_file=None, ai_provider="unsupported")


def test_gemini_api_key_is_optional() -> None:
    settings = Settings(_env_file=None, gemini_api_key=None)

    assert settings.gemini_api_key is None


@pytest.mark.parametrize("model", ["", "   ", "\t"])
def test_gemini_model_must_not_be_empty(model: str) -> None:
    with pytest.raises(ValidationError, match="gemini_model must not be empty"):
        Settings(_env_file=None, gemini_model=model)


def test_existing_openai_settings_remain_compatible() -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="test-openai-key",
        openai_model="gpt-4.1-mini",
    )

    assert settings.ai_provider == "openai"
    assert settings.openai_api_key == "test-openai-key"
    assert settings.openai_model == "gpt-4.1-mini"


def test_google_genai_sdk_can_be_imported() -> None:
    from google import genai

    assert genai is not None


@pytest.mark.parametrize("history_turns", [0, 11])
def test_gemini_history_turn_limit_is_validated(history_turns: int) -> None:
    with pytest.raises(ValidationError, match="gemini_history_turns"):
        Settings(_env_file=None, gemini_history_turns=history_turns)
