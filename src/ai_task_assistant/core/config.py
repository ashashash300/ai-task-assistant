import os
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_settings_env_file = None if os.getenv("AI_TASK_ASSISTANT_DISABLE_DOTENV") == "1" else ".env"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    database_url: str = "sqlite:///./ai_task_assistant.db"
    ai_provider: Literal["openai", "gemini"] = "openai"
    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_history_turns: int = Field(default=5, ge=1, le=10)
    conversation_retention_days: int = Field(default=30, ge=1)
    session_ttl_hours: int = Field(default=24, ge=1, le=24 * 30)
    session_cookie_secure: bool = True
    session_cookie_samesite: Literal["lax", "strict"] = "lax"
    auth_allowed_origins: str = "http://127.0.0.1:5173,http://localhost:5173"
    login_ip_limit: int = Field(default=10, ge=1)
    login_ip_window_seconds: int = Field(default=60, ge=1)
    login_email_limit: int = Field(default=5, ge=1)
    login_email_window_seconds: int = Field(default=300, ge=1)
    register_ip_limit: int = Field(default=5, ge=1)
    register_ip_window_seconds: int = Field(default=3600, ge=1)
    csrf_ip_limit: int = Field(default=30, ge=1)
    csrf_ip_window_seconds: int = Field(default=60, ge=1)
    chat_user_limit: int = Field(default=10, ge=1)
    chat_user_window_seconds: int = Field(default=60, ge=1)
    chat_daily_quota: int = Field(default=50, ge=1)
    chat_concurrent_limit: int = Field(default=1, ge=1, le=10)
    chat_input_max_length: int = Field(default=4000, ge=1, le=20_000)
    provider_max_calls_per_chat: int = Field(default=5, ge=2, le=10)
    provider_max_tool_rounds: int = Field(default=5, ge=1, le=10)
    provider_request_timeout_seconds: int = Field(default=30, ge=1, le=120)
    provider_max_output_tokens: int = Field(default=512, ge=16, le=4096)
    model_config = SettingsConfigDict(env_file=_settings_env_file, extra="ignore")

    @field_validator("gemini_model")
    @classmethod
    def validate_gemini_model(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("gemini_model must not be empty")
        return value

    @field_validator("auth_allowed_origins")
    @classmethod
    def validate_auth_allowed_origins(cls, value: str) -> str:
        origins = [origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()]
        if not origins:
            raise ValueError("auth_allowed_origins must include at least one origin")

        normalized_origins: list[str] = []
        for origin in origins:
            parsed = urlparse(origin)
            is_local_http = parsed.scheme == "http" and parsed.hostname in {
                "localhost",
                "127.0.0.1",
            }
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.params
                or parsed.query
                or parsed.fragment
                or (parsed.scheme != "https" and not is_local_http)
            ):
                raise ValueError("auth_allowed_origins contains an invalid origin")
            normalized_origins.append(origin)
        return ",".join(dict.fromkeys(normalized_origins))

    @property
    def auth_allowed_origin_values(self) -> tuple[str, ...]:
        return tuple(self.auth_allowed_origins.split(","))


settings = Settings()
