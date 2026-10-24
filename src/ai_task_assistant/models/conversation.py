from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.db.base import Base
from ai_task_assistant.models.task import TaipeiDateTime


class Conversation(Base):
    """Persisted provider-specific state for one client conversation."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "conversation_id",
            name="uq_conversations_user_id_conversation_id",
        ),
        Index("ix_conversations_user_id_updated_at", "user_id", "updated_at"),
        CheckConstraint(
            "provider IN ('openai', 'gemini')",
            name="ck_conversations_provider",
        ),
        CheckConstraint("turn_count >= 0", name="ck_conversations_turn_count"),
        CheckConstraint("state_version > 0", name="ck_conversations_state_version"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    openai_response_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gemini_history_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    turn_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, onupdate=current_taipei_time, nullable=False
    )
