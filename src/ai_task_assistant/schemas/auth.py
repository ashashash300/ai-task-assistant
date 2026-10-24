import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ai_task_assistant.core.security import normalize_email


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class CredentialsRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=12, max_length=128)

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized_email = normalize_email(value)
        if not EMAIL_PATTERN.fullmatch(normalized_email):
            raise ValueError("email must be a valid email address")
        return normalized_email


class RegisterRequest(CredentialsRequest):
    pass


class LoginRequest(CredentialsRequest):
    pass


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str


class CsrfTokenResponse(BaseModel):
    csrf_token: str
