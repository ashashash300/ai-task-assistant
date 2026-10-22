import json
import logging
from collections.abc import Callable
from typing import Any

from google import genai
from google.genai import types

from ai_task_assistant.core.config import settings
from ai_task_assistant.schemas.tools import (
    CompleteTaskToolArguments,
    CreateTaskToolArguments,
    DeleteTaskToolArguments,
    ListTasksToolArguments,
    UpdateTaskToolArguments,
)
from ai_task_assistant.services.llm_provider import ProviderError, ProviderResult, ToolExecutor


logger = logging.getLogger(__name__)

GEMINI_TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="create_task",
                description="Create a new todo task when the user asks to add a reminder or task.",
                parameters_json_schema=CreateTaskToolArguments.model_json_schema(),
            ),
            types.FunctionDeclaration(
                name="list_tasks",
                description="List tasks. Optionally filter by status or due-date range.",
                parameters_json_schema=ListTasksToolArguments.model_json_schema(),
            ),
            types.FunctionDeclaration(
                name="update_task",
                description="Update fields on an existing task identified by task_id.",
                parameters_json_schema=UpdateTaskToolArguments.model_json_schema(),
            ),
            types.FunctionDeclaration(
                name="complete_task",
                description="Mark an existing task as completed using task_id.",
                parameters_json_schema=CompleteTaskToolArguments.model_json_schema(),
            ),
            types.FunctionDeclaration(
                name="delete_task",
                description="Request deletion of one task identified by task_id. The backend requires user confirmation before deleting.",
                parameters_json_schema=DeleteTaskToolArguments.model_json_schema(),
            ),
        ]
    )
]


def get_gemini_client() -> genai.Client:
    if not settings.gemini_api_key:
        raise ProviderError("AI service is not configured")
    return genai.Client(
        api_key=settings.gemini_api_key,
        http_options=types.HttpOptions(
            timeout=settings.provider_request_timeout_seconds * 1000,
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def log_gemini_failure(stage: str, error: Exception) -> None:
    """Log safe diagnostic metadata without request content or secrets."""

    status_code = getattr(error, "status_code", None) or getattr(error, "code", None)
    request_id = getattr(error, "request_id", None)
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if request_id is None and headers is not None:
        request_id = headers.get("x-request-id") or headers.get("x-goog-request-id")
    logger.error(
        "Gemini service failure stage=%s exception=%s status_code=%s request_id=%s",
        stage,
        type(error).__name__,
        status_code if status_code is not None else "none",
        request_id if request_id is not None else "none",
    )


def _model_content(response: Any) -> types.Content:
    candidates = getattr(response, "candidates", None) or []
    if candidates and getattr(candidates[0], "content", None) is not None:
        return candidates[0].content

    function_calls = getattr(response, "function_calls", None) or []
    parts = [
        types.Part.from_function_call(
            name=call.name,
            args=call.args if isinstance(call.args, dict) else {},
        )
        for call in function_calls
        if getattr(call, "name", None)
    ]
    if parts:
        return types.Content(role="model", parts=parts)
    return types.Content(role="model", parts=[types.Part.from_text(text=_response_text(response))])


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    if isinstance(text, str):
        return text
    return ""


def _function_calls(response: Any) -> list[Any]:
    function_calls = getattr(response, "function_calls", None)
    if function_calls is not None:
        return list(function_calls)

    candidates = getattr(response, "candidates", None) or []
    if not candidates or getattr(candidates[0], "content", None) is None:
        return []
    return [
        part.function_call
        for part in (candidates[0].content.parts or [])
        if getattr(part, "function_call", None) is not None
    ]


def _is_function_response(content: types.Content) -> bool:
    return any(part.function_response is not None for part in (content.parts or []))


def _is_function_call(content: types.Content) -> bool:
    return any(part.function_call is not None for part in (content.parts or []))


def _split_history_into_turns(history: list[types.Content]) -> list[list[types.Content]]:
    """Group a user turn with its model and tool-response contents."""

    turns: list[list[types.Content]] = []
    for content in history:
        starts_user_turn = content.role == "user" and not _is_function_response(content)
        if starts_user_turn:
            turns.append([content])
        elif turns:
            turns[-1].append(content)
        else:
            raise ProviderError("Conversation context is unavailable. Start a new conversation_id.")
    return turns


def _is_complete_turn(turn: list[types.Content]) -> bool:
    if not turn or turn[0].role != "user" or _is_function_response(turn[0]):
        return False
    if turn[-1].role != "model":
        return False

    for index, content in enumerate(turn):
        if _is_function_call(content):
            if index + 1 >= len(turn) or not _is_function_response(turn[index + 1]):
                return False
        if _is_function_response(content):
            if index == 0 or not _is_function_call(turn[index - 1]):
                return False
    return True


def _trim_history_for_request(
    history: list[types.Content], max_history_turns: int
) -> list[types.Content]:
    """Keep complete recent turns and never split a function call/response pair."""

    turns = _split_history_into_turns(history)
    complete_turns = [turn for turn in turns if _is_complete_turn(turn)]
    incomplete_turns = [turn for turn in turns if not _is_complete_turn(turn)]

    if len(incomplete_turns) > 1 or (incomplete_turns and incomplete_turns[-1] is not turns[-1]):
        raise ProviderError("Conversation context is unavailable. Start a new conversation_id.")

    retained_turns = complete_turns[-max_history_turns:]
    if incomplete_turns:
        retained_turns.extend(incomplete_turns)
    return [content for turn in retained_turns for content in turn]


class GeminiProvider:
    """Gemini Generate Content implementation of the shared provider contract."""

    def __init__(self, client_factory: Callable[[], genai.Client] | None = None) -> None:
        self._client_factory = client_factory

    def run_turn(
        self,
        *,
        message: str,
        system_prompt: str,
        conversation_state: object | None,
        execute_tool: ToolExecutor,
    ) -> ProviderResult:
        client = self._client_factory() if self._client_factory else get_gemini_client()
        if conversation_state is None:
            history: list[types.Content] = []
        elif isinstance(conversation_state, list) and all(
            isinstance(item, types.Content) for item in conversation_state
        ):
            history = [item.model_copy(deep=True) for item in conversation_state]
        else:
            raise ProviderError("Conversation context is unavailable. Start a new conversation_id.")

        history.append(types.Content(role="user", parts=[types.Part.from_text(text=message)]))
        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=GEMINI_TOOLS,
            max_output_tokens=settings.provider_max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        stage = "initial_response"

        try:
            max_rounds = min(
                settings.provider_max_tool_rounds,
                settings.provider_max_calls_per_chat,
            )
            for _ in range(max_rounds):
                history = _trim_history_for_request(history, settings.gemini_history_turns)
                response = client.models.generate_content(
                    model=settings.gemini_model,
                    contents=history,
                    config=config,
                )
                history.append(_model_content(response))
                function_calls = _function_calls(response)
                if not function_calls:
                    return ProviderResult(
                        response=_response_text(response),
                        conversation_state=history,
                    )

                stage = "tool_execution"
                function_response_parts: list[types.Part] = []
                for function_call in function_calls:
                    name = getattr(function_call, "name", None) or ""
                    arguments = getattr(function_call, "args", None)
                    result = execute_tool(name, json.dumps(arguments, ensure_ascii=False))
                    function_response_parts.append(
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=getattr(function_call, "id", None),
                                name=name,
                                response=result,
                            )
                        )
                    )

                history.append(types.Content(role="user", parts=function_response_parts))
                stage = "followup_response"

            raise ProviderError("AI service is temporarily unavailable")
        except ProviderError:
            raise
        except Exception as error:
            log_gemini_failure(stage, error)
            raise ProviderError("AI service is temporarily unavailable") from error
