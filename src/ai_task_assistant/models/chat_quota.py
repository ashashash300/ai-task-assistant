"""Durable per-user, per-Taipei-day chat request reservations."""

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.db.base import Base
from ai_task_assistant.models.task import TaipeiDateTime


class ChatDailyQuota(Base):
    __tablename__ = "chat_daily_quotas"
    __table_args__ = (
        UniqueConstraint("user_id", "quota_date", name="uq_chat_daily_quotas_user_date"),
        CheckConstraint("request_count >= 0", name="ck_chat_daily_quotas_request_count"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    quota_date: Mapped[date] = mapped_column(Date, nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TaipeiDateTime(), default=current_taipei_time, onupdate=current_taipei_time, nullable=False
    )
