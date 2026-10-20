from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_task_assistant.core.datetime import normalize_to_taipei
from ai_task_assistant.models.task import TaskPriority, TaskStatus
from ai_task_assistant.schemas.task import TaskUpdate


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @staticmethod
    def normalize_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return normalize_to_taipei(value)


class CreateTaskToolArguments(ToolArguments):
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    due_date: datetime | None = None
    priority: TaskPriority = TaskPriority.MEDIUM

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be empty")
        return value

    @field_validator("due_date")
    @classmethod
    def normalize_due_date(cls, value: datetime | None) -> datetime | None:
        return cls.normalize_datetime(value)


class ListTasksToolArguments(ToolArguments):
    status: TaskStatus | None = None
    due_after: datetime | None = None
    due_before: datetime | None = None

    @field_validator("due_after", "due_before")
    @classmethod
    def normalize_filter_dates(cls, value: datetime | None) -> datetime | None:
        return cls.normalize_datetime(value)


class UpdateTaskToolArguments(ToolArguments):
    task_id: int = Field(gt=0)
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
        return cls.normalize_datetime(value)

    @model_validator(mode="after")
    def require_update_field(self) -> "UpdateTaskToolArguments":
        update_fields = {"title", "description", "due_date", "priority", "status"}
        if not self.model_fields_set.intersection(update_fields):
            raise ValueError("at least one field must be provided for update")
        return self

    def to_task_update(self) -> TaskUpdate:
        values = self.model_dump(exclude={"task_id"}, exclude_unset=True)
        return TaskUpdate.model_validate(values)


class CompleteTaskToolArguments(ToolArguments):
    task_id: int = Field(gt=0)
