"""SQLite-backed daily chat quota reservations.

Reservation commits before an external provider request. A provider failure still
uses that reservation, and no SQLite transaction remains open while the provider
is running.
"""

from datetime import date, datetime, time, timedelta
import math

from sqlalchemy import delete
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai_task_assistant.core.datetime import current_taipei_time, normalize_to_taipei
from ai_task_assistant.models.chat_quota import ChatDailyQuota


class ChatQuotaExceeded(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("Chat quota exceeded")


class ChatQuotaStorageError(Exception):
    """A quota storage failure must prevent an AI provider call."""


def _retry_after_next_taipei_day(now: datetime) -> int:
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=now.tzinfo)
    return max(1, math.ceil((midnight - now).total_seconds()))


def reserve_daily_chat_quota(
    db: Session,
    *,
    user_id: int,
    daily_limit: int,
    now: datetime | None = None,
) -> int:
    """Atomically reserve one durable daily request, or fail without provider access."""

    reservation_time = normalize_to_taipei(now) if now is not None else current_taipei_time()
    quota_date = reservation_time.date()
    statement = (
        sqlite_insert(ChatDailyQuota)
        .values(
            user_id=user_id,
            quota_date=quota_date,
            request_count=1,
            created_at=reservation_time,
            updated_at=reservation_time,
        )
        .on_conflict_do_update(
            index_elements=[ChatDailyQuota.user_id, ChatDailyQuota.quota_date],
            set_={
                "request_count": ChatDailyQuota.request_count + 1,
                "updated_at": reservation_time,
            },
            where=ChatDailyQuota.request_count < daily_limit,
        )
        .returning(ChatDailyQuota.request_count)
    )
    try:
        count = db.scalar(statement)
        if count is None:
            db.rollback()
            raise ChatQuotaExceeded(_retry_after_next_taipei_day(reservation_time))
        db.commit()
        return count
    except ChatQuotaExceeded:
        raise
    except SQLAlchemyError as error:
        db.rollback()
        raise ChatQuotaStorageError("Chat quota is temporarily unavailable") from error


def cleanup_expired_chat_quotas(db: Session, *, before_date: date) -> int:
    """Optional maintenance helper; never invoked on the chat request path."""

    try:
        result = db.execute(delete(ChatDailyQuota).where(ChatDailyQuota.quota_date < before_date))
        db.commit()
        return result.rowcount or 0
    except SQLAlchemyError as error:
        db.rollback()
        raise ChatQuotaStorageError("Chat quota cleanup is temporarily unavailable") from error
