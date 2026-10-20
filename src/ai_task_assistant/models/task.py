from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, Enum as SqlEnum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from ai_task_assistant.core.datetime import (
    current_taipei_time,
    from_sqlite_datetime,
    to_sqlite_datetime,
)
from ai_task_assistant.db.base import Base


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TaskStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TaipeiDateTime(TypeDecorator[datetime]):
    """Store Taipei local time as naive SQLite DATETIME and restore its timezone."""

    impl = DateTime(timezone=False)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        return to_sqlite_datetime(value)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return from_sqlite_datetime(value)


class Task(Base):
    """A todo task stored in the local SQLite database."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    due_date: Mapped[datetime | None] = mapped_column(TaipeiDateTime(), nullable=True)
    priority: Mapped[TaskPriority] = mapped_column(
        SqlEnum(TaskPriority, native_enum=False),
        default=TaskPriority.MEDIUM,
        nullable=False,
    )
    status: Mapped[TaskStatus] = mapped_column(
        SqlEnum(TaskStatus, native_enum=False),
        default=TaskStatus.PENDING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, onupdate=current_taipei_time, nullable=False
    )
