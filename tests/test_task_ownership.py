"""Owner-scoped Task, Conversation, and chat authorization regression tests."""

from collections.abc import Generator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.api.dependencies.auth import SESSION_COOKIE_NAME
from ai_task_assistant.core.config import settings
from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.db.base import Base
from ai_task_assistant.db.schema import SchemaCompatibilityError, assert_ownership_schema_compatible
from ai_task_assistant.db.session import get_db
import ai_task_assistant.main as main_module
from ai_task_assistant.main import app
from ai_task_assistant.models.conversation import Conversation
from ai_task_assistant.models.task import Task
from ai_task_assistant.models.user import User
from ai_task_assistant.schemas.task import TaskCreate, TaskUpdate
from ai_task_assistant.services import conversation_service, task_service
from ai_task_assistant.services.ai_service import execute_tool


ORIGIN = "http://127.0.0.1:5173"


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
    app.dependency_overrides[get_db] = override_get_db
    original_engine = main_module.engine
    main_module.engine = engine
    conversation_service.clear_pending_deletions()
    try:
        with TestClient(app) as test_client:
            test_client.ownership_session_factory = factory
            test_client.ownership_engine = engine
            yield test_client
    finally:
        main_module.engine = original_engine
        app.dependency_overrides.clear()
        conversation_service.clear_pending_deletions()
        Base.metadata.drop_all(engine)


def csrf_headers(client: TestClient) -> dict[str, str]:
    response = client.get("/auth/csrf", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def register_and_login(client: TestClient, email: str) -> str:
    password = "long-safe-password"
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


def create_owned_task(client: TestClient, title: str) -> dict:
    response = client.post("/tasks", headers=csrf_headers(client), json={"title": title})
    assert response.status_code == 201
    return response.json()


def test_tasks_and_task_mutations_require_authenticated_session(client: TestClient) -> None:
    assert client.get("/tasks").status_code == 401
    assert client.post("/tasks", json={"title": "blocked"}).status_code == 401
    assert client.post("/chat", json={"message": "blocked"}).status_code == 401


def test_task_crud_is_fully_scoped_to_the_authenticated_user(client: TestClient) -> None:
    token_a = register_and_login(client, "a@example.com")
    task_a = create_owned_task(client, "A task")
    token_b = register_and_login(client, "b@example.com")
    task_b = create_owned_task(client, "B task")

    use_session(client, token_a)
    assert [task["id"] for task in client.get("/tasks").json()] == [task_a["id"]]
    assert client.get(f"/tasks/{task_b['id']}").status_code == 404
    assert client.patch(
        f"/tasks/{task_b['id']}", headers=csrf_headers(client), json={"title": "stolen"}
    ).status_code == 404
    assert client.delete(f"/tasks/{task_b['id']}", headers=csrf_headers(client)).status_code == 404

    use_session(client, token_b)
    assert [task["id"] for task in client.get("/tasks").json()] == [task_b["id"]]
    assert client.get(f"/tasks/{task_a['id']}").status_code == 404

    with client.ownership_session_factory() as db:
        assert db.get(Task, task_a["id"]).title == "A task"
        assert db.get(Task, task_b["id"]).title == "B task"


def test_task_schema_and_tool_arguments_cannot_set_owner(client: TestClient) -> None:
    token = register_and_login(client, "schema@example.com")
    use_session(client, token)

    created = client.post(
        "/tasks", headers=csrf_headers(client), json={"title": "x", "user_id": 999}
    )
    assert created.status_code == 201
    with client.ownership_session_factory() as db:
        user = db.query(User).filter_by(email="schema@example.com").one()
        assert db.get(Task, created.json()["id"]).user_id == user.id
        result = execute_tool(
            db,
            "create_task",
            '{"title":"tool", "user_id":999}',
            user_id=user.id,
        )
        assert result == {"ok": False, "error": "Invalid tool arguments"}


def test_tool_call_and_prompt_like_task_id_cannot_cross_user_scope(client: TestClient) -> None:
    token_a = register_and_login(client, "tool-a@example.com")
    task_a = create_owned_task(client, "private")
    token_b = register_and_login(client, "tool-b@example.com")
    use_session(client, token_b)

    with client.ownership_session_factory() as db:
        user_b = db.query(User).filter_by(email="tool-b@example.com").one()
        result = execute_tool(
            db,
            "update_task",
            f'{{"task_id": {task_a["id"]}, "title": "ignore authorization"}}',
            user_id=user_b.id,
        )
        assert result == {"ok": False, "error": "Task not found"}
        assert task_service.get_task(db, user_id=user_b.id, task_id=task_a["id"]) is None
    use_session(client, token_a)
    assert client.get(f"/tasks/{task_a['id']}").json()["title"] == "private"


def test_conversations_and_pending_deletions_are_isolated_per_user(client: TestClient) -> None:
    token_a = register_and_login(client, "conversation-a@example.com")
    token_b = register_and_login(client, "conversation-b@example.com")
    with client.ownership_session_factory() as db:
        user_a = db.query(User).filter_by(email="conversation-a@example.com").one()
        user_b = db.query(User).filter_by(email="conversation-b@example.com").one()
        state_a = conversation_service.save_response(
            db, user_id=user_a.id, conversation_id="same-id", provider_state="resp-a",
            provider="openai", expected_state_version=None, expected_row_id=None,
        )
        state_b = conversation_service.save_response(
            db, user_id=user_b.id, conversation_id="same-id", provider_state="resp-b",
            provider="openai", expected_state_version=None, expected_row_id=None,
        )
        assert state_a.row_id != state_b.row_id
        assert conversation_service.get_conversation(
            db, user_id=user_a.id, conversation_id="same-id"
        ).provider_state == "resp-a"
        assert conversation_service.get_conversation(
            db, user_id=user_b.id, conversation_id="same-id"
        ).provider_state == "resp-b"
        conversation_service.save_pending_deletion(
            user_id=user_a.id,
            conversation_id="same-id",
            request=conversation_service.PendingDeletionRequest(task_id=1, title="A only"),
        )
        assert conversation_service.get_pending_deletion(
            user_id=user_b.id, conversation_id="same-id"
        ) is None

    use_session(client, token_a)
    assert client.get("/auth/me").status_code == 200
    use_session(client, token_b)
    assert client.get("/auth/me").status_code == 200


def test_optimistic_locking_row_id_prevents_aba_revival(client: TestClient) -> None:
    with client.ownership_session_factory() as db:
        user = User(email="aba@example.com", password_hash="test-hash")
        db.add(user)
        db.commit()
        original = conversation_service.save_response(
            db, user_id=user.id, conversation_id="aba", provider_state="resp-old",
            provider="openai", expected_state_version=None, expected_row_id=None,
        )
        db.delete(db.get(Conversation, original.row_id))
        db.commit()
        replacement = conversation_service.save_response(
            db, user_id=user.id, conversation_id="aba", provider_state="resp-new",
            provider="openai", expected_state_version=None, expected_row_id=None,
        )
        with pytest.raises(conversation_service.ConversationConflictError):
            conversation_service.save_response(
                db, user_id=user.id, conversation_id="aba", provider_state="resp-stale",
                provider="openai", expected_state_version=original.state_version,
                expected_row_id=original.row_id,
            )
        assert replacement.row_id != original.row_id


def test_csrf_and_origin_are_required_for_task_and_chat_mutations(client: TestClient) -> None:
    token = register_and_login(client, "csrf-owner@example.com")
    use_session(client, token)

    assert client.post("/tasks", json={"title": "missing csrf"}).status_code == 403
    assert client.post(
        "/tasks", headers={"Origin": "https://attacker.example", "X-CSRF-Token": "x"},
        json={"title": "wrong origin"},
    ).status_code == 403
    assert client.post("/chat", json={"message": "missing csrf"}).status_code == 403


def test_conversation_limits_and_retention_are_scoped_per_user(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with client.ownership_session_factory() as db:
        user_a = User(email="limit-a@example.com", password_hash="test-hash")
        user_b = User(email="limit-b@example.com", password_hash="test-hash")
        db.add_all([user_a, user_b])
        db.commit()
        conversation_service.save_response(
            db, user_id=user_a.id, conversation_id="limit", provider_state="a",
            provider="openai", expected_state_version=None, expected_row_id=None,
        )
        monkeypatch.setattr(conversation_service, "MAX_CONVERSATIONS", 1)
        with pytest.raises(conversation_service.ConversationLimitError):
            conversation_service.prepare_conversation(
                db, user_id=user_a.id, conversation_id="another", provider="openai"
            )
        assert conversation_service.prepare_conversation(
            db, user_id=user_b.id, conversation_id="another", provider="openai"
        ) is None
        stale = Conversation(
            user_id=user_a.id,
            conversation_id="expired",
            provider="openai",
            openai_response_id="old",
            updated_at=current_taipei_time() - timedelta(days=31),
        )
        db.add(stale)
        db.commit()
        assert conversation_service.cleanup_expired_conversations(
            db, user_id=user_b.id, now=current_taipei_time()
        ) == 0
        assert db.get(Conversation, stale.id) is not None


def test_owner_scoped_orm_schema_and_foreign_keys(client: TestClient) -> None:
    engine = client.ownership_engine
    inspector = inspect(engine)
    assert {"ix_tasks_user_id_id", "ix_tasks_user_id_due_date"}.issubset(
        {index["name"] for index in inspector.get_indexes("tasks")}
    )
    assert "ix_conversations_user_id_updated_at" in {
        index["name"] for index in inspector.get_indexes("conversations")
    }
    with client.ownership_session_factory() as db:
        db.add(Task(user_id=999999, title="invalid owner"))
        with pytest.raises(Exception):
            db.commit()
        db.rollback()
        with pytest.raises(TypeError):
            task_service.create_task(db, task_in=TaskCreate(title="missing owner"))


def test_legacy_schema_is_rejected_without_writing(tmp_path) -> None:
    database_path = tmp_path / "legacy.sqlite"
    engine = create_engine(f"sqlite:///{database_path}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL)")
        connection.exec_driver_sql("CREATE TABLE conversations (id INTEGER PRIMARY KEY, conversation_id TEXT)")
    before = database_path.read_bytes()

    with pytest.raises(SchemaCompatibilityError, match="incompatible"):
        assert_ownership_schema_compatible(engine)

    assert database_path.read_bytes() == before
    engine.dispose()
