from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


ToolExecutor = Callable[[str, object], dict[str, Any]]


class ProviderError(Exception):
    """A safe provider error that can be surfaced by the shared AI service."""


class ProviderContextError(ProviderError):
    """Raised when a provider-specific conversation state cannot be continued."""


class ProviderNotImplementedError(ProviderError):
    """Raised when a configured provider has not been implemented yet."""


@dataclass
class ProviderResult:
    response: str
    conversation_state: object | None = None


class LLMProvider(Protocol):
    """Minimal interface for providers that can run one task-assistant turn."""

    def run_turn(
        self,
        *,
        message: str,
        system_prompt: str,
        conversation_state: object | None,
        execute_tool: ToolExecutor,
    ) -> ProviderResult: ...
