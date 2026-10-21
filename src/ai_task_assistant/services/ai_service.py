import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError
from sqlalchemy.orm import Session

from ai_task_assistant.core.config import settings
from ai_task_assistant.core.datetime import current_taipei_time, from_sqlite_datetime
from ai_task_assistant.models.task import TaskStatus
from ai_task_assistant.schemas.task import TaskCreate, TaskUpdate
from ai_task_assistant.schemas.tools import (
    CompleteTaskToolArguments,
    CreateTaskToolArguments,
    DeleteTaskToolArguments,
    ListTasksToolArguments,
    UpdateTaskToolArguments,
)
from ai_task_assistant.services import task_service
from ai_task_assistant.services.conversation_service import PendingDeletionRequest
from ai_task_assistant.services.llm_provider import (
    LLMProvider,
    ProviderContextError,
    ProviderError,
    ProviderNotImplementedError,
)
from ai_task_assistant.services.gemini_provider import GeminiProvider
from ai_task_assistant.services.openai_provider import OpenAIProvider


class AIServiceError(Exception):
    """A safe error that can be returned by the chat route."""


class ConversationContextError(AIServiceError):
    """A stored remote response chain can no longer be continued."""


@dataclass
class ChatResult:
    response: str
    conversation_state: object | None
    pending_deletion: PendingDeletionRequest | None = None


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


def execute_tool(
    db: Session,
    name: str,
    raw_arguments: str,
    conversation_id: str | None = None,
    pending_deletion: list[PendingDeletionRequest | None] | None = None,
) -> dict[str, Any]:
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

        if name == "delete_task":
            args = DeleteTaskToolArguments.model_validate(arguments)
            if conversation_id is None or pending_deletion is None:
                return {
                    "ok": False,
                    "error": "A conversation_id is required to request task deletion",
                }
            if pending_deletion[0] is not None:
                return {"ok": False, "error": "Only one task deletion can be requested at a time"}
            task = task_service.get_task(db, args.task_id)
            if task is None:
                return {"ok": False, "error": "Task not found"}
            pending_deletion[0] = PendingDeletionRequest(task_id=task.id, title=task.title)
            return {
                "ok": True,
                "confirmation_required": True,
                "task": {"id": task.id, "title": task.title},
            }
    except ValidationError:
        return {"ok": False, "error": "Invalid tool arguments"}

    return {"ok": False, "error": "Unknown tool"}


def get_provider() -> LLMProvider:
    if settings.ai_provider == "openai":
        return OpenAIProvider()
    if settings.ai_provider == "gemini":
        return GeminiProvider()
    raise ProviderNotImplementedError("AI provider is not implemented")


def run_chat(
    db: Session,
    message: str,
    conversation_state: object | None = None,
    conversation_id: str | None = None,
) -> ChatResult:
    """Run one provider turn without giving the LLM database access."""

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
        " For delete_task, only request deletion for an explicit task_id. The backend will ask "
        "the user to confirm and will not delete the task from this tool call."
    )
    pending_deletion: list[PendingDeletionRequest | None] = [None]
    try:
        result = get_provider().run_turn(
            message=message,
            system_prompt=system_prompt,
            conversation_state=conversation_state,
            execute_tool=lambda name, raw_arguments: execute_tool(
                db,
                name,
                raw_arguments,
                conversation_id=conversation_id,
                pending_deletion=pending_deletion,
            ),
        )
        return ChatResult(
            response=result.response,
            conversation_state=result.conversation_state,
            pending_deletion=pending_deletion[0],
        )
    except ProviderContextError as error:
        raise ConversationContextError(str(error)) from error
    except ProviderError as error:
        raise AIServiceError(str(error)) from error
