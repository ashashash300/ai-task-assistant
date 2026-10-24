"""Loopback-only backend entry point for the deterministic interview demo.

This is intentionally separate from the normal FastAPI startup path. It only
installs the demo provider after every demo-specific runtime guard has passed.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_DATABASE_PATH = (REPOSITORY_ROOT / "ai_task_assistant.db").resolve()
PLACEHOLDER_OPENAI_KEY = "demo-placeholder-openai-key"
PLACEHOLDER_GEMINI_KEY = "demo-placeholder-gemini-key"


class DemoConfigurationError(RuntimeError):
    """Raised before importing application code when demo configuration is unsafe."""


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise DemoConfigurationError(f"Missing required demo setting: {name}")
    return value


def _parse_port(name: str) -> int:
    try:
        port = int(_required(name))
    except ValueError as error:
        raise DemoConfigurationError(f"Invalid demo port: {name}") from error
    if not 1 <= port <= 65_535:
        raise DemoConfigurationError(f"Invalid demo port: {name}")
    return port


def _absolute_path(name: str) -> Path:
    path = Path(_required(name)).expanduser()
    if not path.is_absolute():
        raise DemoConfigurationError(f"Demo path must be absolute: {name}")
    return path.resolve()


def _sqlite_url(path: Path) -> str:
    return f"sqlite:///{path}"


def validate_demo_environment() -> tuple[Path, int]:
    """Validate explicit demo-only settings before application imports occur."""

    if os.getenv("AI_TASK_ASSISTANT_DEMO") != "1":
        raise DemoConfigurationError("Demo mode is not enabled")
    try:
        uuid.UUID(_required("AI_TASK_ASSISTANT_DEMO_INSTANCE_ID"))
    except ValueError as error:
        raise DemoConfigurationError("Demo instance identifier is invalid") from error

    if os.getenv("AI_TASK_ASSISTANT_DISABLE_DOTENV") != "1":
        raise DemoConfigurationError("Demo mode must disable dotenv loading")
    if os.getenv("AI_PROVIDER") != "openai":
        raise DemoConfigurationError("Demo mode requires the isolated mock provider configuration")
    if os.getenv("OPENAI_API_KEY") != PLACEHOLDER_OPENAI_KEY:
        raise DemoConfigurationError("Demo mode requires the OpenAI placeholder key")
    if os.getenv("GEMINI_API_KEY") != PLACEHOLDER_GEMINI_KEY:
        raise DemoConfigurationError("Demo mode requires the Gemini placeholder key")
    if os.getenv("SESSION_COOKIE_SECURE") != "false":
        raise DemoConfigurationError("Loopback demo requires an explicitly non-secure local cookie")

    temp_directory = _absolute_path("AI_TASK_ASSISTANT_DEMO_TEMP_DIR")
    database_path = _absolute_path("AI_TASK_ASSISTANT_DEMO_DATABASE_PATH")
    if database_path == DEVELOPMENT_DATABASE_PATH:
        raise DemoConfigurationError("Demo database must not be the development database")
    try:
        database_path.relative_to(temp_directory)
    except ValueError as error:
        raise DemoConfigurationError("Demo database must be inside the demo temporary directory") from error
    if not temp_directory.name.startswith("ai-task-assistant-demo-"):
        raise DemoConfigurationError("Demo temporary directory name is invalid")
    if os.getenv("DATABASE_URL") != _sqlite_url(database_path):
        raise DemoConfigurationError("Demo database URL does not match the isolated database path")

    backend_port = _parse_port("AI_TASK_ASSISTANT_DEMO_BACKEND_PORT")
    frontend_port = _parse_port("AI_TASK_ASSISTANT_DEMO_FRONTEND_PORT")
    expected_origin = f"http://127.0.0.1:{frontend_port}"
    if os.getenv("AUTH_ALLOWED_ORIGINS") != expected_origin:
        raise DemoConfigurationError("Demo Origin must match the exact loopback Vite Origin")
    if backend_port == frontend_port:
        raise DemoConfigurationError("Demo backend and frontend ports must differ")
    return database_path, backend_port


def main() -> None:
    try:
        _database_path, backend_port = validate_demo_environment()
    except DemoConfigurationError as error:
        print(f"demo_server: configuration rejected: {error}", file=sys.stderr)
        raise SystemExit(2) from error

    from ai_task_assistant.services import ai_service
    from ai_task_assistant.services.demo_provider import DeterministicDemoProvider

    # This mutation is process-local and exists only in this explicitly guarded
    # entry point. Normal ``ai_task_assistant.main`` startup never imports it.
    ai_service.get_provider = lambda: DeterministicDemoProvider()

    from ai_task_assistant.main import app
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=backend_port, log_level="warning")


if __name__ == "__main__":
    main()
