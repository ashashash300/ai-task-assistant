import json
from dataclasses import dataclass
from typing import Any

from openai import BadRequestError, NotFoundError, OpenAI
from pydantic import ValidationError
from sqlalchemy.orm import Session

from ai_task_assistant.core.config import settings
from ai_task_assistant.core.datetime import current_taipei_time, from_sqlite_datetime
from ai_task_assistant.models.task import TaskStatus
from ai_task_assistant.schemas.task import TaskCreate, TaskUpdate
from ai_task_assistant.schemas.tools import (
    CompleteTaskToolArguments,
    CreateTaskToolArguments,
    ListTasksToolArguments,
    UpdateTaskToolArguments,
)
from ai_task_assistant.services import task_service


class AIServiceError(Exception):
    """A safe error that can be returned by the chat route."""


class ConversationContextError(AIServiceError):
    """A stored remote response chain can no longer be continued."""


@dataclass
class ChatResult:
    response: str
    response_id: str


TOOL_DEFINITIONS = [
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
]


def get_openai_client() -> OpenAI:
    if not settings.openai_api_key:
        raise AIServiceError("AI service is not configured")
    return OpenAI(api_key=settings.openai_api_key)


def serialize_task(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "due_date": from_sqlite_datetime(task.due_date).isoformat() if task.due_date else None,
        "priority": task.priority.value,
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def execute_tool(db: Session, name: str, raw_arguments: str) -> dict[str, Any]:
    """Validate a tool call and delegate all task work to Task Service."""

    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {"ok": False, "error": "Invalid tool arguments"}

    try:
        if name == "create_task":
            args = CreateTaskToolArguments.model_validate(arguments)
            task = task_service.create_task(
                db,
                TaskCreate(
                    title=args.title,
                    description=args.description,
                    due_date=args.due_date,
                    priority=args.priority,
                ),
            )
            return {"ok": True, "task": serialize_task(task)}

        if name == "list_tasks":
            args = ListTasksToolArguments.model_validate(arguments)
            tasks = task_service.list_tasks(
                db,
                status=args.status,
                due_after=args.due_after,
                due_before=args.due_before,
            )
            return {"ok": True, "tasks": [serialize_task(task) for task in tasks]}

        if name == "update_task":
            args = UpdateTaskToolArguments.model_validate(arguments)
            task = task_service.get_task(db, args.task_id)
            if task is None:
                return {"ok": False, "error": "Task not found"}
            updated_task = task_service.update_task(db, task, args.to_task_update())
            return {"ok": True, "task": serialize_task(updated_task)}

        if name == "complete_task":
            args = CompleteTaskToolArguments.model_validate(arguments)
            task = task_service.get_task(db, args.task_id)
            if task is None:
                return {"ok": False, "error": "Task not found"}
            completed_task = task_service.update_task(
                db,
                task,
                TaskUpdate(status=TaskStatus.COMPLETED),
            )
            return {"ok": True, "task": serialize_task(completed_task)}
    except ValidationError:
        return {"ok": False, "error": "Invalid tool arguments"}

    return {"ok": False, "error": "Unknown tool"}


def run_chat(
    db: Session, message: str, previous_response_id: str | None = None
) -> ChatResult:
    """Run one tool-calling turn without giving the LLM database access."""

    client = get_openai_client()
    current_time = current_taipei_time().isoformat()
    system_prompt = (
        "You are a helpful task assistant. Use the available tools for task "
        "operations. Respond in the user's language after receiving tool results. "
        f"The current date and time in Asia/Taipei is {current_time}. "
        "Interpret 明天下午三點 as tomorrow at 15:00 Asia/Taipei and "
        "明天晚上八點 as tomorrow at 20:00 Asia/Taipei. 下週五 means Friday "
        "of the next calendar week. If the user says only 明天, 後天, or 下週五 "
        "without a time, ask for a time and do not call a task tool. When calling "
        "a tool, due_date must be an ISO 8601 datetime with the +08:00 offset. "
        "If required task details are missing, or a task reference is ambiguous, ask "
        "a clarifying question and do not create, update, complete, or delete a task."
    )
    conversation: list[Any] = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {"role": "user", "content": message},
    ]

    try:
        request_options: dict[str, Any] = {
            "model": settings.openai_model,
            "input": conversation,
            "tools": TOOL_DEFINITIONS,
        }
        if previous_response_id is not None:
            request_options["previous_response_id"] = previous_response_id

        response = client.responses.create(
            **request_options
        )
        tool_calls = [item for item in response.output if item.type == "function_call"]
        if not tool_calls:
            return ChatResult(response=response.output_text, response_id=response.id)

        tool_outputs: list[Any] = []
        for tool_call in tool_calls:
            result = execute_tool(db, tool_call.name, tool_call.arguments)
            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                }
            )

        final_response = client.responses.create(
            model=settings.openai_model,
            input=[{"role": "system", "content": system_prompt}, *tool_outputs],
            tools=TOOL_DEFINITIONS,
            previous_response_id=response.id,
        )
        return ChatResult(response=final_response.output_text, response_id=final_response.id)
    except AIServiceError:
        raise
    except (BadRequestError, NotFoundError) as error:
        if previous_response_id is not None:
            raise ConversationContextError(
                "Conversation context is unavailable. Start a new conversation_id."
            ) from error
        raise AIServiceError("AI service is temporarily unavailable") from error
    except Exception as error:
        raise AIServiceError("AI service is temporarily unavailable") from error
