from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.db.base import Base
from ai_task_assistant.models.task import TaipeiDateTime


class UserSession(Base):
    """A future server-side session that stores only an opaque token digest."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TaipeiDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(TaipeiDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, nullable=False
    )
