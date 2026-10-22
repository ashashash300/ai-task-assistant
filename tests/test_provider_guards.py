from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_task_assistant.core.config import settings
from ai_task_assistant.schemas.chat import ChatRequest
from ai_task_assistant.services import gemini_provider, openai_provider
from ai_task_assistant.services.llm_provider import ProviderError


class OpenAIResponses:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_chat_input_limit_uses_configured_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "chat_input_max_length", 3)
    with pytest.raises(ValidationError, match="configured maximum"):
        ChatRequest(message="four")


def test_openai_sets_output_limit_and_hard_timeout_without_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "openai_api_key", "test-key")
    monkeypatch.setattr(settings, "provider_request_timeout_seconds", 17)
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(openai_provider, "OpenAI", FakeOpenAI)
    openai_provider.get_openai_client()
    assert captured["timeout"] == 17
    assert captured["max_retries"] == 0

    responses = OpenAIResponses([
        SimpleNamespace(output=[], output_text="done", id="final-id"),
    ])
    provider = openai_provider.OpenAIProvider(client_factory=lambda: SimpleNamespace(responses=responses))
    monkeypatch.setattr(settings, "provider_max_output_tokens", 123)
    result = provider.run_turn(message="hello", system_prompt="system", conversation_state=None, execute_tool=lambda *_: {})
    assert result.response == "done"
    assert responses.calls[0]["max_output_tokens"] == 123


def test_gemini_tool_round_limit_stops_unbounded_tool_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                function_calls=[SimpleNamespace(name="list_tasks", args={}, id="call-1")],
                candidates=[],
                text="",
            )

    provider = gemini_provider.GeminiProvider(client_factory=lambda: SimpleNamespace(models=Models()))
    monkeypatch.setattr(settings, "provider_max_tool_rounds", 1)
    monkeypatch.setattr(settings, "provider_max_calls_per_chat", 2)
    monkeypatch.setattr(settings, "provider_max_output_tokens", 222)
    with pytest.raises(ProviderError, match="temporarily unavailable"):
        provider.run_turn(message="list", system_prompt="system", conversation_state=None, execute_tool=lambda *_: {"ok": True})
    assert len(calls) == 1
    assert calls[0]["config"].max_output_tokens == 222


def test_gemini_provider_call_limit_stops_before_another_remote_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class Models:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                function_calls=[SimpleNamespace(name="list_tasks", args={}, id=f"call-{len(calls)}")],
                candidates=[],
                text="",
            )

    provider = gemini_provider.GeminiProvider(client_factory=lambda: SimpleNamespace(models=Models()))
    monkeypatch.setattr(settings, "provider_max_tool_rounds", 5)
    monkeypatch.setattr(settings, "provider_max_calls_per_chat", 2)
    with pytest.raises(ProviderError, match="temporarily unavailable"):
        provider.run_turn(message="list", system_prompt="system", conversation_state=None, execute_tool=lambda *_: {"ok": True})
    assert len(calls) == 2


def test_gemini_client_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(settings, "provider_request_timeout_seconds", 19)
    captured: dict[str, object] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(gemini_provider.genai, "Client", FakeClient)
    gemini_provider.get_gemini_client()
    assert captured["http_options"].timeout == 19_000
    assert captured["http_options"].retry_options.attempts == 1
