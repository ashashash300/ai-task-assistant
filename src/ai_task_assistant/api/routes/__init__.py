"""API route modules."""

from ai_task_assistant.api.routes.auth import router as auth_router
from ai_task_assistant.api.routes.tasks import router as tasks_router
from ai_task_assistant.api.routes.chat import router as chat_router

__all__ = ["auth_router", "chat_router", "tasks_router"]
