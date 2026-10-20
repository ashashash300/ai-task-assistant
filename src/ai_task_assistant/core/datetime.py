from datetime import datetime
from zoneinfo import ZoneInfo


TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")


def current_taipei_time() -> datetime:
    """Return the current timezone-aware time in Asia/Taipei."""

    return datetime.now(TAIPEI_TIMEZONE)


def normalize_to_taipei(value: datetime) -> datetime:
    """Require an aware datetime and convert it to Asia/Taipei."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("due_date must include a timezone offset")
    return value.astimezone(TAIPEI_TIMEZONE)


def to_sqlite_datetime(value: datetime | None) -> datetime | None:
    """Convert an aware datetime to SQLite's naive Asia/Taipei representation."""

    if value is None:
        return None
    return normalize_to_taipei(value).replace(tzinfo=None)


def from_sqlite_datetime(value: datetime | None) -> datetime | None:
    """Restore SQLite's naive Asia/Taipei representation to an aware datetime."""

    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=TAIPEI_TIMEZONE)
    return value.astimezone(TAIPEI_TIMEZONE)
