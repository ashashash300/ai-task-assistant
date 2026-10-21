import pytest

from ai_task_assistant.core.config import settings
from ai_task_assistant.services.ai_service import get_provider
from ai_task_assistant.services import gemini_provider, openai_provider


def test_openai_provider_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_provider", "openai")

    assert isinstance(get_provider(), openai_provider.OpenAIProvider)


def test_gemini_provider_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_provider", "gemini")

    assert isinstance(get_provider(), gemini_provider.GeminiProvider)


def test_openai_tool_definitions_include_delete_task() -> None:
    names = [tool["name"] for tool in openai_provider.OPENAI_TOOL_DEFINITIONS]

    assert "delete_task" in names
