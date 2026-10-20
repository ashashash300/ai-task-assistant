from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_task_assistant.core.datetime import normalize_to_taipei
from ai_task_assistant.models.task import TaskPriority, TaskStatus


class TaskFields(BaseModel):
    """Fields shared by task creation and updates."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    due_date: datetime | None = None
    priority: TaskPriority | None = None
    status: TaskStatus | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return value

        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value

    @field_validator("due_date")
    @classmethod
    def normalize_due_date(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return normalize_to_taipei(value)


class TaskCreate(TaskFields):
    title: str = Field(min_length=1, max_length=255)
    priority: TaskPriority = TaskPriority.MEDIUM
    status: TaskStatus = TaskStatus.PENDING


class TaskUpdate(TaskFields):
    pass


class TaskResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    due_date: datetime | None
    priority: TaskPriority
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
