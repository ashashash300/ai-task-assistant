from dataclasses import dataclass
from datetime import datetime, timedelta

from ai_task_assistant.core.datetime import current_taipei_time


MAX_CONVERSATIONS = 100
MAX_TURNS_PER_CONVERSATION = 10
PENDING_DELETION_TTL = timedelta(minutes=5)


class ConversationLimitError(Exception):
    """Raised when a conversation cannot accept another turn."""


class ConversationProviderMismatchError(Exception):
    """Raised when a conversation is resumed with another LLM provider."""


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
    last_response_id: str | None
    turn_count: int
    updated_at: datetime
    provider: str = "openai"
    provider_state: object | None = None


_conversations: dict[str, ConversationState] = {}
_pending_deletions: dict[str, PendingDeletionState] = {}


def get_conversation(conversation_id: str) -> ConversationState | None:
    return _conversations.get(conversation_id)


def prepare_conversation(
    conversation_id: str, provider: str | None = None
) -> ConversationState | None:
    """Check limits without changing state before an LLM request succeeds."""

    state = get_conversation(conversation_id)
    if state is not None:
        if provider is not None and state.provider != provider:
            raise ConversationProviderMismatchError(
                "Conversation provider does not match. Start a new conversation_id."
            )
        if state.turn_count >= MAX_TURNS_PER_CONVERSATION:
            raise ConversationLimitError(
                "Conversation turn limit reached. Start a new conversation_id."
            )
        return state

    if len(_conversations) >= MAX_CONVERSATIONS:
        raise ConversationLimitError(
            "Conversation limit reached. Use an existing conversation_id or restart the application."
        )
    return None


def save_response(
    conversation_id: str,
    provider_state: object,
    provider: str = "openai",
) -> ConversationState:
    """Save only a completed final response after a successful chat turn."""

    existing_state = get_conversation(conversation_id)
    state = ConversationState(
        last_response_id=provider_state if provider == "openai" and isinstance(provider_state, str) else None,
        turn_count=(existing_state.turn_count + 1) if existing_state else 1,
        updated_at=current_taipei_time(),
        provider=provider,
        provider_state=provider_state,
    )
    _conversations[conversation_id] = state
    return state


def save_pending_deletion(
    conversation_id: str, request: PendingDeletionRequest
) -> PendingDeletionState:
    """Save a deletion request only after its LLM turn completed successfully."""

    state = PendingDeletionState(
        task_id=request.task_id,
        title=request.title,
        expires_at=current_taipei_time() + PENDING_DELETION_TTL,
    )
    _pending_deletions[conversation_id] = state
    return state


def get_pending_deletion(conversation_id: str) -> PendingDeletionState | None:
    """Return an unexpired request only while its conversation state exists."""

    pending = _pending_deletions.get(conversation_id)
    if pending is None:
        return None
    if conversation_id not in _conversations or pending.expires_at <= current_taipei_time():
        _pending_deletions.pop(conversation_id, None)
        return None
    return pending


def clear_pending_deletion(conversation_id: str) -> None:
    _pending_deletions.pop(conversation_id, None)


def clear_conversations() -> None:
    """Clear in-memory state. Used by tests and lost when the app restarts."""

    _conversations.clear()
    _pending_deletions.clear()
