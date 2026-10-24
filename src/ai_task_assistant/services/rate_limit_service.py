"""Single-process, concurrency-safe request and chat guards.

Durable per-day chat reservations are intentionally implemented separately in
``chat_quota_service``. This in-memory service still resets on restart and is
not shared by workers or replicas, so it is not a complete multi-process rate
limit or billing system.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import hmac
import math
import secrets
from threading import Lock
from time import monotonic

from fastapi import Request

_EMAIL_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class RateLimitExceeded(Exception):
    retry_after: int
    detail: str = "Too many requests"


class InMemoryRateLimiter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._in_flight_chat: dict[int, int] = defaultdict(int)

    @staticmethod
    def _retry_after(events: deque[float], now: float, window_seconds: int) -> int:
        return max(1, math.ceil(events[0] + window_seconds - now))

    @staticmethod
    def _prune(events: deque[float], now: float, window_seconds: int) -> None:
        while events and now - events[0] >= window_seconds:
            events.popleft()

    def consume(self, *, key: str, limit: int, window_seconds: int) -> None:
        now = monotonic()
        with self._lock:
            events = self._events[key]
            self._prune(events, now, window_seconds)
            if len(events) >= limit:
                raise RateLimitExceeded(self._retry_after(events, now, window_seconds))
            events.append(now)

    def acquire_chat(
        self,
        *,
        user_id: int,
        minute_limit: int,
        minute_window_seconds: int,
        concurrent_limit: int,
    ) -> None:
        now_monotonic = monotonic()
        key = f"chat:user:{user_id}:minute"
        with self._lock:
            events = self._events[key]
            self._prune(events, now_monotonic, minute_window_seconds)
            if len(events) >= minute_limit:
                raise RateLimitExceeded(self._retry_after(events, now_monotonic, minute_window_seconds))
            if self._in_flight_chat[user_id] >= concurrent_limit:
                raise RateLimitExceeded(1, "Too many requests")
            events.append(now_monotonic)
            self._in_flight_chat[user_id] += 1

    def release_chat(self, *, user_id: int) -> None:
        with self._lock:
            current = self._in_flight_chat.get(user_id, 0)
            if current <= 1:
                self._in_flight_chat.pop(user_id, None)
            else:
                self._in_flight_chat[user_id] = current - 1

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._in_flight_chat.clear()


rate_limiter = InMemoryRateLimiter()


def client_ip(request: Request) -> str:
    """Use the direct ASGI peer only; never trust arbitrary forwarded headers."""

    return request.client.host if request.client is not None else "unknown"


def email_limiter_key(normalized_email: str) -> str:
    digest = hmac.new(_EMAIL_KEY, normalized_email.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"login:email:{digest}"


def clear_rate_limits() -> None:
    """Test-only reset for process-local state."""

    rate_limiter.clear()
