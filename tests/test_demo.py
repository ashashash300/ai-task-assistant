"""Regression tests for the explicitly isolated deterministic demo path."""

from collections.abc import Generator
import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.api.dependencies.auth import SESSION_COOKIE_NAME
from ai_task_assistant.core.config import settings
from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import get_db
import ai_task_assistant.main as main_module
from ai_task_assistant.main import app
from ai_task_assistant.models.task import TaskStatus
from ai_task_assistant.services import ai_service
from ai_task_assistant.services.demo_provider import (
    COMPLETE_DEMO_TASK_COMMAND,
    CREATE_DEMO_TASK_COMMAND,
    DEMO_TASK_DESCRIPTION,
    DEMO_TASK_TITLE,
    DeterministicDemoProvider,
)
from ai_task_assistant.services.openai_provider import OpenAIProvider


ORIGIN = "http://127.0.0.1:5173"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(engine)

    def override_get_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    monkeypatch.setattr(settings, "session_cookie_secure", False)
    monkeypatch.setattr(settings, "chat_daily_quota", 5)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(ai_service, "get_provider", lambda: DeterministicDemoProvider())
    app.dependency_overrides[get_db] = override_get_db
    original_engine = main_module.engine
    main_module.engine = engine
    try:
        with TestClient(app) as test_client:
            test_client.demo_session_factory = factory
            yield test_client
    finally:
        main_module.engine = original_engine
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)


def csrf_headers(client: TestClient) -> dict[str, str]:
    response = client.get("/auth/csrf", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def register_and_login(client: TestClient, email: str) -> str:
    password = "deterministic-demo-password"
    assert client.post(
        "/auth/register", headers=csrf_headers(client), json={"email": email, "password": password}
    ).status_code == 201
    assert client.post(
        "/auth/login", headers=csrf_headers(client), json={"email": email, "password": password}
    ).status_code == 200
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token is not None
    return token


def use_session(client: TestClient, token: str) -> None:
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE_NAME, token)


def chat_headers(client: TestClient) -> dict[str, str]:
    return {**csrf_headers(client), "Content-Type": "application/json"}


def test_demo_server_fails_closed_without_explicit_mode() -> None:
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": "src",
        "AI_TASK_ASSISTANT_DISABLE_DOTENV": "1",
    }
    result = subprocess.run(
        [sys.executable, "scripts/demo_server.py"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Demo mode is not enabled" in result.stderr


def test_normal_provider_factory_never_installs_demo_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setenv("AI_TASK_ASSISTANT_DEMO", "1")

    assert isinstance(ai_service.get_provider(), OpenAIProvider)


def test_demo_create_and_complete_use_validated_authenticated_tools(client: TestClient) -> None:
    register_and_login(client, "demo-a@example.com")

    created = client.post(
        "/chat",
        headers=chat_headers(client),
        json={"conversation_id": "demo-flow", "message": CREATE_DEMO_TASK_COMMAND},
    )
    assert created.status_code == 200, created.text
    assert created.json()["response"] == "已建立「準備 AI 面試」任務，優先順序為高。"

    tasks = client.get("/tasks").json()
    assert len(tasks) == 1
    assert tasks[0]["title"] == DEMO_TASK_TITLE
    assert tasks[0]["description"] == DEMO_TASK_DESCRIPTION
    assert tasks[0]["status"] == TaskStatus.PENDING.value

    completed = client.post(
        "/chat",
        headers=chat_headers(client),
        json={"conversation_id": "demo-flow", "message": COMPLETE_DEMO_TASK_COMMAND},
    )
    assert completed.status_code == 200
    assert completed.json()["response"] == "已完成剛剛建立的「準備 AI 面試」任務。"
    assert client.get("/tasks").json()[0]["status"] == TaskStatus.COMPLETED.value


def test_demo_completion_is_scoped_to_the_authenticated_account(client: TestClient) -> None:
    token_a = register_and_login(client, "demo-owner-a@example.com")
    assert client.post(
        "/chat",
        headers=chat_headers(client),
        json={"conversation_id": "demo-a", "message": CREATE_DEMO_TASK_COMMAND},
    ).status_code == 200

    token_b = register_and_login(client, "demo-owner-b@example.com")
    use_session(client, token_b)
    completed = client.post(
        "/chat",
        headers=chat_headers(client),
        json={"conversation_id": "demo-b", "message": COMPLETE_DEMO_TASK_COMMAND},
    )
    assert completed.status_code == 200
    assert "找不到可完成的 demo 任務" in completed.json()["response"]
    assert client.get("/tasks").json() == []

    use_session(client, token_a)
    tasks_a = client.get("/tasks").json()
    assert len(tasks_a) == 1
    assert tasks_a[0]["status"] == TaskStatus.PENDING.value


def test_unsupported_demo_command_cannot_trigger_tool_mutation() -> None:
    calls: list[tuple[str, object]] = []
    result = DeterministicDemoProvider().run_turn(
        message="刪除所有任務",
        system_prompt="unused",
        conversation_state=None,
        execute_tool=lambda name, arguments: calls.append((name, arguments)) or {"ok": True},
    )

    assert calls == []
    assert "deterministic demo" in result.response


def test_demo_provider_never_constructs_real_provider_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    import ai_task_assistant.services.gemini_provider as gemini_provider
    import ai_task_assistant.services.openai_provider as openai_provider

    monkeypatch.setattr(
        openai_provider,
        "get_openai_client",
        lambda: pytest.fail("demo provider attempted to construct OpenAI client"),
    )
    monkeypatch.setattr(
        gemini_provider,
        "get_gemini_client",
        lambda: pytest.fail("demo provider attempted to construct Gemini client"),
    )

    result = DeterministicDemoProvider().run_turn(
        message=CREATE_DEMO_TASK_COMMAND,
        system_prompt="unused",
        conversation_state=None,
        execute_tool=lambda _name, _arguments: {"ok": True},
    )
    assert result.response.startswith("已建立")


def _load_demo_launcher_module():
    module_path = REPOSITORY_ROOT / "scripts" / "run_demo.py"
    spec = importlib.util.spec_from_file_location("demo_launcher_test_module", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_demo_cleanup_only_removes_owned_temporary_directory(tmp_path: Path) -> None:
    launcher = _load_demo_launcher_module()
    owned_directory = tmp_path / "ai-task-assistant-demo-owned"
    owned_directory.mkdir()
    (owned_directory / "demo.sqlite3").write_text("temporary")
    unrelated_directory = tmp_path / "unrelated-data"
    unrelated_directory.mkdir()
    (unrelated_directory / "keep.txt").write_text("keep")

    launcher.cleanup_demo_directory(owned_directory)
    launcher.cleanup_demo_directory(unrelated_directory)

    assert not owned_directory.exists()
    assert (unrelated_directory / "keep.txt").read_text() == "keep"


def test_demo_database_guard_rejects_development_and_nonowned_paths(tmp_path: Path) -> None:
    launcher = _load_demo_launcher_module()

    with pytest.raises(launcher.DemoLaunchError):
        launcher.assert_safe_demo_database_path(launcher.DEVELOPMENT_DATABASE_PATH)
    with pytest.raises(launcher.DemoLaunchError):
        launcher.assert_safe_demo_database_path(tmp_path / "other.sqlite3")
