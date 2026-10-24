import re

from pydantic import BaseModel, Field, field_validator

from ai_task_assistant.core.config import settings


CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ChatRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1)

    @field_validator("conversation_id")
    @classmethod
    def validate_conversation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not CONVERSATION_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "conversation_id must use letters, numbers, hyphens, or underscores"
            )
        return value

    @field_validator("message")
    @classmethod
    def strip_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        if len(value) > settings.chat_input_max_length:
            raise ValueError("message exceeds the configured maximum length")
        return value


class ChatResponse(BaseModel):
    conversation_id: str | None = None
    response: str
