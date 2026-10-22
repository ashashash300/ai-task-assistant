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


def test_conversation_retention_defaults_to_thirty_days() -> None:
    settings = Settings(_env_file=None)

    assert settings.conversation_retention_days == 30


@pytest.mark.parametrize("retention_days", [0, -1])
def test_conversation_retention_must_be_positive(retention_days: int) -> None:
    with pytest.raises(ValidationError, match="conversation_retention_days"):
        Settings(_env_file=None, conversation_retention_days=retention_days)


@pytest.mark.parametrize("history_turns", [0, 11])
def test_gemini_history_turn_limit_is_validated(history_turns: int) -> None:
    with pytest.raises(ValidationError, match="gemini_history_turns"):
        Settings(_env_file=None, gemini_history_turns=history_turns)


def test_authentication_session_settings_have_safe_defaults() -> None:
    settings = Settings(_env_file=None)

    assert settings.session_ttl_hours == 24
    assert settings.session_cookie_secure is True
    assert settings.session_cookie_samesite == "lax"


@pytest.mark.parametrize("ttl_hours", [0, -1, 721])
def test_session_ttl_must_be_in_safe_range(ttl_hours: int) -> None:
    with pytest.raises(ValidationError, match="session_ttl_hours"):
        Settings(_env_file=None, session_ttl_hours=ttl_hours)


@pytest.mark.parametrize(
    "origins",
    ["", "https://example.com/path", "http://example.com", "not-a-url"],
)
def test_auth_allowed_origins_rejects_invalid_values(origins: str) -> None:
    with pytest.raises(ValidationError, match="auth_allowed_origins"):
        Settings(_env_file=None, auth_allowed_origins=origins)


def test_auth_allowed_origins_are_normalized() -> None:
    settings = Settings(
        _env_file=None,
        auth_allowed_origins=" https://app.example.com/ , https://app.example.com ",
    )

    assert settings.auth_allowed_origin_values == ("https://app.example.com",)
