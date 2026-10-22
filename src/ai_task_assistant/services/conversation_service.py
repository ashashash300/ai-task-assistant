from dataclasses import dataclass
from datetime import datetime, timedelta
from threading import Lock
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_task_assistant.core.config import settings
from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.models.conversation import Conversation
from ai_task_assistant.services.conversation_codec import (
    ConversationCodecError,
    decode_gemini_history,
    encode_gemini_history,
)


MAX_CONVERSATIONS = 100
MAX_TURNS_PER_CONVERSATION = 10
PENDING_DELETION_TTL = timedelta(minutes=5)


class ConversationLimitError(Exception):
    """Raised when a conversation cannot accept another turn."""


class ConversationProviderMismatchError(Exception):
    """Raised when a conversation is resumed with another LLM provider."""


class ConversationStateError(Exception):
    """Raised when persisted conversation state cannot be safely used."""


class ConversationConflictError(Exception):
    """Raised when another request changed a conversation before it was saved."""


class ConversationStorageError(Exception):
    """Raised when SQLite cannot safely persist conversation state."""


@dataclass(frozen=True)
class PendingDeletionRequest:
    task_id: int
    title: str


@dataclass
class PendingDeletionState:
    task_id: int
    title: str
    expires_at: datetime


@dataclass
class ConversationState:
    row_id: int
    last_response_id: str | None
    turn_count: int
    updated_at: datetime
    provider: str
    provider_state: object
    state_version: int


# Pending deletion authorization is intentionally process-local and expires on restart.
_pending_deletions: dict[tuple[int, str], PendingDeletionState] = {}
_pending_deletions_lock = Lock()


def _state_from_model(conversation: Conversation) -> ConversationState:
    if conversation.provider == "openai":
        if not conversation.openai_response_id:
            raise ConversationStateError(
                "Conversation context is unavailable. Start a new conversation_id."
            )
        provider_state: object = conversation.openai_response_id
        last_response_id = conversation.openai_response_id
    elif conversation.provider == "gemini":
        if conversation.gemini_history_json is None:
            raise ConversationStateError(
                "Conversation context is unavailable. Start a new conversation_id."
            )
        try:
            provider_state = decode_gemini_history(conversation.gemini_history_json)
        except ConversationCodecError as error:
            raise ConversationStateError(
                "Conversation context is unavailable. Start a new conversation_id."
            ) from error
        last_response_id = None
    else:
        raise ConversationStateError(
            "Conversation context is unavailable. Start a new conversation_id."
        )

    return ConversationState(
        row_id=conversation.id,
        last_response_id=last_response_id,
        turn_count=conversation.turn_count,
        updated_at=conversation.updated_at,
        provider=conversation.provider,
        provider_state=provider_state,
        state_version=conversation.state_version,
    )


def get_conversation(db: Session, *, user_id: int, conversation_id: str) -> ConversationState | None:
    """Read one persisted conversation without keeping an in-memory duplicate."""

    try:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.conversation_id == conversation_id,
            )
        )
    except SQLAlchemyError as error:
        raise ConversationStorageError("Conversation state is temporarily unavailable") from error

    if conversation is None:
        return None
    return _state_from_model(conversation)


def _retention_cutoff(now: datetime) -> datetime:
    return now - timedelta(days=settings.conversation_retention_days)


def cleanup_expired_conversations(
    db: Session, *, user_id: int, now: datetime | None = None
) -> int:
    """Delete only conversations strictly older than the configured retention cutoff."""

    cleanup_time = now or current_taipei_time()
    cutoff = _retention_cutoff(cleanup_time)
    try:
        result = db.execute(
            delete(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.updated_at < cutoff,
            )
        )
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise ConversationStorageError("Conversation cleanup is temporarily unavailable") from error
    return result.rowcount or 0


def prepare_conversation(
    db: Session, *, user_id: int, conversation_id: str, provider: str
) -> ConversationState | None:
    """Load and validate a persisted conversation before an LLM request begins."""

    now = current_taipei_time()
    try:
        conversation = db.scalar(
            select(Conversation).where(
                Conversation.user_id == user_id,
                Conversation.conversation_id == conversation_id,
            )
        )
    except SQLAlchemyError as error:
        raise ConversationStorageError("Conversation state is temporarily unavailable") from error

    expired_conversation = False
    if conversation is not None and conversation.updated_at < _retention_cutoff(now):
        cleanup_expired_conversations(db, user_id=user_id, now=now)
        try:
            remaining_conversation = db.scalar(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.conversation_id == conversation_id,
                )
            )
        except SQLAlchemyError as error:
            raise ConversationStorageError("Conversation state is temporarily unavailable") from error
        if remaining_conversation is not None:
            raise ConversationConflictError(
                "Conversation was updated or removed by another request. Retry the request."
            )
        conversation = None
        expired_conversation = True

    if conversation is not None:
        if conversation.provider != provider:
            raise ConversationProviderMismatchError(
                "Conversation provider does not match. Start a new conversation_id."
            )
        if conversation.turn_count >= MAX_TURNS_PER_CONVERSATION:
            raise ConversationLimitError(
                "Conversation turn limit reached. Start a new conversation_id."
            )
        return _state_from_model(conversation)

    # New conversations trigger cleanup before the limit check, then release the
    # short transaction before the external LLM request begins.
    if not expired_conversation:
        cleanup_expired_conversations(db, user_id=user_id, now=now)

    try:
        conversation_count = (
            db.scalar(select(func.count(Conversation.id)).where(Conversation.user_id == user_id))
            or 0
        )
    except SQLAlchemyError as error:
        raise ConversationStorageError("Conversation state is temporarily unavailable") from error
    if conversation_count >= MAX_CONVERSATIONS:
        raise ConversationLimitError(
            "Conversation limit reached. Use an existing conversation_id or restart the application."
        )
    return None


def _serialized_provider_state(provider: str, provider_state: object) -> dict[str, Any]:
    if provider == "openai":
        if not isinstance(provider_state, str) or not provider_state:
            raise ConversationStorageError("Conversation state could not be saved")
        return {
            "openai_response_id": provider_state,
            "gemini_history_json": None,
        }
    if provider == "gemini":
        if not isinstance(provider_state, list):
            raise ConversationStorageError("Conversation state could not be saved")
        try:
            history = encode_gemini_history(provider_state)
        except ConversationCodecError as error:
            raise ConversationStorageError("Conversation state could not be saved") from error
        return {
            "openai_response_id": None,
            "gemini_history_json": history,
        }
    raise ConversationStorageError("Conversation state could not be saved")


def save_response(
    db: Session,
    *,
    user_id: int,
    conversation_id: str,
    provider_state: object,
    provider: str,
    expected_state_version: int | None,
    expected_row_id: int | None,
) -> ConversationState:
    """Persist a completed final response using optimistic concurrency control."""

    serialized_state = _serialized_provider_state(provider, provider_state)
    now = current_taipei_time()

    if expected_state_version is None:
        if expected_row_id is not None:
            raise ConversationStorageError("Conversation state could not be saved")
        conversation = Conversation(
            user_id=user_id,
            conversation_id=conversation_id,
            provider=provider,
            turn_count=1,
            created_at=now,
            updated_at=now,
            **serialized_state,
        )
        try:
            db.add(conversation)
            db.commit()
        except IntegrityError as error:
            db.rollback()
            raise ConversationConflictError(
                "Conversation was created by another request. Retry the request."
            ) from error
        except SQLAlchemyError as error:
            db.rollback()
            raise ConversationStorageError("Conversation state could not be saved") from error
        return _state_from_model(conversation)

    if expected_row_id is None:
        raise ConversationStorageError("Conversation state could not be saved")

    try:
        result = db.execute(
            update(Conversation)
            .where(
                Conversation.id == expected_row_id,
                Conversation.user_id == user_id,
                Conversation.conversation_id == conversation_id,
                Conversation.provider == provider,
                Conversation.state_version == expected_state_version,
            )
            .values(
                **serialized_state,
                turn_count=Conversation.turn_count + 1,
                state_version=Conversation.state_version + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            db.rollback()
            raise ConversationConflictError(
                "Conversation was updated by another request. Retry the request."
            )
        db.commit()
    except ConversationConflictError:
        raise
    except SQLAlchemyError as error:
        db.rollback()
        raise ConversationStorageError("Conversation state could not be saved") from error

    state = get_conversation(db, user_id=user_id, conversation_id=conversation_id)
    if state is None:
        raise ConversationStorageError("Conversation state could not be saved")
    return state


def save_pending_deletion(
    *, user_id: int, conversation_id: str, request: PendingDeletionRequest
) -> PendingDeletionState:
    """Save a request only in memory after its LLM turn completed successfully."""

    state = PendingDeletionState(
        task_id=request.task_id,
        title=request.title,
        expires_at=current_taipei_time() + PENDING_DELETION_TTL,
    )
    with _pending_deletions_lock:
        _pending_deletions[(user_id, conversation_id)] = state
    return state


def get_pending_deletion(*, user_id: int, conversation_id: str) -> PendingDeletionState | None:
    """Return an unexpired in-memory deletion request for this application process."""

    key = (user_id, conversation_id)
    with _pending_deletions_lock:
        pending = _pending_deletions.get(key)
        if pending is None:
            return None
        if pending.expires_at <= current_taipei_time():
            _pending_deletions.pop(key, None)
            return None
        return pending


def consume_pending_deletion(*, user_id: int, conversation_id: str) -> PendingDeletionState | None:
    """Atomically return and consume one unexpired deletion authorization.

    This protects concurrent requests in this single application process. Pending
    authorizations deliberately remain process-local and are not a cross-process
    coordination mechanism.
    """

    key = (user_id, conversation_id)
    with _pending_deletions_lock:
        pending = _pending_deletions.get(key)
        if pending is None:
            return None
        if pending.expires_at <= current_taipei_time():
            _pending_deletions.pop(key, None)
            return None
        return _pending_deletions.pop(key)


def clear_pending_deletion(*, user_id: int, conversation_id: str) -> None:
    with _pending_deletions_lock:
        _pending_deletions.pop((user_id, conversation_id), None)


def clear_pending_deletions() -> None:
    """Clear process-local pending deletion state. Used only by tests."""

    with _pending_deletions_lock:
        _pending_deletions.clear()
