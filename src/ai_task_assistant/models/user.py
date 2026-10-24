from datetime import datetime

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, validates

from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.core.security import normalize_email
from ai_task_assistant.db.base import Base
from ai_task_assistant.models.task import TaipeiDateTime


class User(Base):
    """A future authenticated account; password hashes are never returned by APIs."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(),
        default=current_taipei_time,
        onupdate=current_taipei_time,
        nullable=False,
    )

    @validates("email")
    def normalize_user_email(self, _key: str, value: str) -> str:
        return normalize_email(value)
