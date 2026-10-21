import json
import logging
from collections.abc import Callable
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)

from ai_task_assistant.core.config import settings
from ai_task_assistant.schemas.tools import (
    CompleteTaskToolArguments,
    CreateTaskToolArguments,
    DeleteTaskToolArguments,
    ListTasksToolArguments,
    UpdateTaskToolArguments,
)
from ai_task_assistant.services.llm_provider import (
    ProviderContextError,
    ProviderError,
    ProviderResult,
    ToolExecutor,
)


logger = logging.getLogger(__name__)


OPENAI_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "name": "create_task",
        "description": "Create a new todo task when the user asks to add a reminder or task.",
        "parameters": CreateTaskToolArguments.model_json_schema(),
    },
    {
        "type": "function",
        "name": "list_tasks",
        "description": "List tasks. Optionally filter by status or due-date range.",
        "parameters": ListTasksToolArguments.model_json_schema(),
    },
    {
        "type": "function",
        "name": "update_task",
        "description": "Update fields on an existing task identified by task_id.",
        "parameters": UpdateTaskToolArguments.model_json_schema(),
    },
    {
        "type": "function",
        "name": "complete_task",
        "description": "Mark an existing task as completed using task_id.",
        "parameters": CompleteTaskToolArguments.model_json_schema(),
    },
    {
        "type": "function",
        "name": "delete_task",
        "description": "Request deletion of one task identified by task_id. The backend requires user confirmation before deleting.",
        "parameters": DeleteTaskToolArguments.model_json_schema(),
    },
]


def get_openai_client() -> OpenAI:
    if not settings.openai_api_key:
        raise ProviderError("AI service is not configured")
    return OpenAI(api_key=settings.openai_api_key)


def log_ai_failure(stage: str, error: Exception) -> None:
    """Log safe diagnostic metadata without request content or secrets."""

    status_code = getattr(error, "status_code", None)
    request_id = getattr(error, "request_id", None)
    logger.error(
        "AI service failure stage=%s exception=%s status_code=%s request_id=%s",
        stage,
        type(error).__name__,
        status_code if status_code is not None else "none",
        request_id if request_id is not None else "none",
    )


class OpenAIProvider:
    """OpenAI Responses API implementation of the shared provider contract."""

    def __init__(self, client_factory: Callable[[], OpenAI] | None = None) -> None:
        self._client_factory = client_factory

    def run_turn(
        self,
        *,
        message: str,
        system_prompt: str,
        conversation_state: object | None,
        execute_tool: ToolExecutor,
    ) -> ProviderResult:
        if conversation_state is not None and not isinstance(conversation_state, str):
            raise ProviderContextError(
                "Conversation context is unavailable. Start a new conversation_id."
            )
        client = self._client_factory() if self._client_factory else get_openai_client()
        stage = "initial_response"

        try:
            conversation: list[Any] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ]
            request_options: dict[str, Any] = {
                "model": settings.openai_model,
                "input": conversation,
                "tools": OPENAI_TOOL_DEFINITIONS,
            }
            if conversation_state is not None:
                request_options["previous_response_id"] = conversation_state

            response = client.responses.create(**request_options)
            tool_calls = [item for item in response.output if item.type == "function_call"]
            if not tool_calls:
                return ProviderResult(
                    response=response.output_text,
                    conversation_state=response.id,
                )

            tool_outputs: list[Any] = []
            for tool_call in tool_calls:
                stage = "tool_execution"
                result = execute_tool(tool_call.name, tool_call.arguments)
                tool_outputs.append(
                    {
                        "type": "function_call_output",
                        "call_id": tool_call.call_id,
                        "output": json.dumps(result, ensure_ascii=False),
                    }
                )

            stage = "followup_response"
            final_response = client.responses.create(
                model=settings.openai_model,
                input=[{"role": "system", "content": system_prompt}, *tool_outputs],
                tools=OPENAI_TOOL_DEFINITIONS,
                previous_response_id=response.id,
            )
            return ProviderResult(
                response=final_response.output_text,
                conversation_state=final_response.id,
            )
        except ProviderError:
            raise
        except APITimeoutError as error:
            log_ai_failure(stage, error)
            raise ProviderError("AI service is temporarily unavailable") from error
        except APIConnectionError as error:
            log_ai_failure(stage, error)
            raise ProviderError("AI service is temporarily unavailable") from error
        except AuthenticationError as error:
            log_ai_failure(stage, error)
            raise ProviderError("AI service is temporarily unavailable") from error
        except PermissionDeniedError as error:
            log_ai_failure(stage, error)
            raise ProviderError("AI service is temporarily unavailable") from error
        except RateLimitError as error:
            log_ai_failure(stage, error)
            raise ProviderError("AI service is temporarily unavailable") from error
        except (BadRequestError, NotFoundError) as error:
            log_ai_failure(stage, error)
            if conversation_state is not None:
                raise ProviderContextError(
                    "Conversation context is unavailable. Start a new conversation_id."
                ) from error
            raise ProviderError("AI service is temporarily unavailable") from error
        except Exception as error:
            log_ai_failure(stage, error)
            raise ProviderError("AI service is temporarily unavailable") from error
