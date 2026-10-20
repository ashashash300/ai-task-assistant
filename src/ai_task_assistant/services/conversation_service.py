from dataclasses import dataclass
from datetime import datetime

from ai_task_assistant.core.datetime import current_taipei_time


MAX_CONVERSATIONS = 100
MAX_TURNS_PER_CONVERSATION = 10


class ConversationLimitError(Exception):
    """Raised when a conversation cannot accept another turn."""


@dataclass
class ConversationState:
    last_response_id: str
    turn_count: int
    updated_at: datetime


_conversations: dict[str, ConversationState] = {}


def get_conversation(conversation_id: str) -> ConversationState | None:
    return _conversations.get(conversation_id)


def prepare_conversation(conversation_id: str) -> ConversationState | None:
    """Check limits without changing state before an LLM request succeeds."""

    state = get_conversation(conversation_id)
    if state is not None:
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


def save_response(conversation_id: str, response_id: str) -> ConversationState:
    """Save only a completed final response after a successful chat turn."""

    existing_state = get_conversation(conversation_id)
    state = ConversationState(
        last_response_id=response_id,
        turn_count=(existing_state.turn_count + 1) if existing_state else 1,
        updated_at=current_taipei_time(),
    )
    _conversations[conversation_id] = state
    return state


def clear_conversations() -> None:
    """Clear in-memory state. Used by tests and lost when the app restarts."""

    _conversations.clear()
