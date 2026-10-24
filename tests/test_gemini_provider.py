import logging
from collections.abc import Generator
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from google.genai import types
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.api.dependencies.auth import require_csrf_protection, require_current_user
from ai_task_assistant.core.config import settings
from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import get_db
import ai_task_assistant.main as main_module
from ai_task_assistant.main import app
from ai_task_assistant.models.conversation import Conversation
from ai_task_assistant.models.user import User
from ai_task_assistant.services import ai_service, conversation_service, gemini_provider


class MockGeminiModels:
    def __init__(self, responses: list[object]) -> None:
        self.responses_to_return = responses
        self.calls: list[dict] = []

    def generate_content(self, **kwargs):
        call = dict(kwargs)
        call["contents"] = list(kwargs["contents"])
        self.calls.append(call)
        response = self.responses_to_return.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MockGeminiClient:
    def __init__(self, responses: list[object]) -> None:
        self.models = MockGeminiModels(responses)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_session = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=test_engine)
    with test_session() as db:
        user = User(email="gemini@example.com", password_hash="test-hash")
        db.add(user)
        db.commit()
        db.refresh(user)

    def override_get_db() -> Generator[Session, None, None]:
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    conversation_service.clear_pending_deletions()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_current_user] = lambda: user
    app.dependency_overrides[require_csrf_protection] = lambda: None
    original_engine = main_module.engine
    main_module.engine = test_engine
    try:
        with TestClient(app) as test_client:
            test_client.conversation_session_factory = test_session
            yield test_client
    finally:
        main_module.engine = original_engine
        app.dependency_overrides.clear()
        conversation_service.clear_pending_deletions()
        Base.metadata.drop_all(bind=test_engine)


def gemini_text(text: str) -> object:
    return SimpleNamespace(function_calls=[], candidates=[], text=text)


def gemini_tool_calls(*calls: tuple[str, object]) -> object:
    return SimpleNamespace(
        function_calls=[
            SimpleNamespace(name=name, args=arguments, id=f"call-{index}")
            for index, (name, arguments) in enumerate(calls)
        ],
        candidates=[],
        text="",
    )


def content_texts(contents: list[object]) -> list[str]:
    return [
        part.text
        for content in contents
        for part in (content.parts or [])
        if part.text is not None
    ]


def mock_gemini(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> MockGeminiClient:
    mock_client = MockGeminiClient(responses)
    monkeypatch.setattr(settings, "ai_provider", "gemini")
    monkeypatch.setattr(gemini_provider, "get_gemini_client", lambda: mock_client)
    return mock_client


def get_persisted_conversation(client: TestClient, conversation_id: str):
    with client.conversation_session_factory() as db:
        return conversation_service.get_conversation(db, user_id=1, conversation_id=conversation_id)


def test_gemini_plain_text_response_uses_manual_tool_execution_config(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_gemini(monkeypatch, [gemini_text("您好")])

    response = client.post("/chat", json={"message": "你好"})

    assert response.status_code == 200
    assert response.json() == {"response": "您好"}
    config = mock_client.models.calls[0]["config"]
    assert config.automatic_function_calling.disable is True
    assert len(config.tools) == 1


@pytest.mark.parametrize(
    ("tool_name", "arguments", "message", "expected_field", "expected_value"),
    [
        ("create_task", {"title": "Gemini task"}, "建立任務", "title", "Gemini task"),
        ("list_tasks", {}, "列出任務", "title", "Existing task"),
        ("update_task", {"task_id": 1, "priority": "high"}, "提高優先度", "priority", "high"),
        ("complete_task", {"task_id": 1}, "完成任務", "status", "completed"),
    ],
)
def test_gemini_tools_delegate_to_existing_task_service(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
    arguments: dict,
    message: str,
    expected_field: str,
    expected_value: str,
) -> None:
    if tool_name != "create_task":
        client.post("/tasks", json={"title": "Existing task"})
    mock_client = mock_gemini(
        monkeypatch,
        [gemini_tool_calls((tool_name, arguments)), gemini_text("已完成。")],
    )

    response = client.post("/chat", json={"message": message})

    assert response.status_code == 200
    assert response.json() == {"response": "已完成。"}
    tasks = client.get("/tasks").json()
    assert tasks[0][expected_field] == expected_value
    assert len(mock_client.models.calls) == 2


def test_gemini_executes_each_parallel_function_call_once(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_gemini(
        monkeypatch,
        [
            gemini_tool_calls(
                ("create_task", {"title": "First"}),
                ("create_task", {"title": "Second"}),
            ),
            gemini_text("已建立兩項任務。"),
        ],
    )

    response = client.post("/chat", json={"message": "建立兩項任務"})

    assert response.status_code == 200
    assert [task["title"] for task in client.get("/tasks").json()] == ["First", "Second"]
    assert len(mock_client.models.calls) == 2


def test_gemini_delete_tool_creates_confirmation_without_deleting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = client.post("/tasks", json={"title": "Gemini delete"}).json()
    mock_gemini(
        monkeypatch,
        [gemini_tool_calls(("delete_task", {"task_id": task["id"]})), gemini_text("請確認刪除。")],
    )

    requested = client.post(
        "/chat", json={"conversation_id": "gemini-delete", "message": "刪除任務"}
    )

    assert requested.status_code == 200
    assert requested.json()["response"] == "請確認是否刪除「Gemini delete」？"
    assert client.get(f"/tasks/{task['id']}").status_code == 200

    confirmed = client.post(
        "/chat", json={"conversation_id": "gemini-delete", "message": "確認"}
    )

    assert confirmed.status_code == 200
    assert client.get(f"/tasks/{task['id']}").status_code == 404


def test_persisted_gemini_delete_history_cannot_authorize_after_restart(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only process-local pending state can authorize the second deletion step."""

    task = client.post("/tasks", json={"title": "Persisted Gemini delete"}).json()
    history = [
        types.Content(role="user", parts=[types.Part.from_text(text="刪除任務")]),
        types.Content(
            role="model",
            parts=[
                types.Part.from_function_call(
                    name="delete_task", args={"task_id": task["id"]}
                )
            ],
        ),
        types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="delete_task",
                        response={"ok": True, "task_id": task["id"]},
                    )
                )
            ],
        ),
        types.Content(role="model", parts=[types.Part.from_text(text="請確認刪除。")]),
    ]
    with client.conversation_session_factory() as db:
        conversation_service.save_response(
            db,
            user_id=1,
            conversation_id="persisted-gemini-delete",
            provider_state=history,
            provider="gemini",
            expected_state_version=None,
            expected_row_id=None,
        )
    # The fixture starts with an empty pending-deletion store, equivalent to a restart.
    mock_gemini(monkeypatch, [gemini_text("請重新提出刪除要求。")])

    response = client.post(
        "/chat",
        json={"conversation_id": "persisted-gemini-delete", "message": "確認"},
    )

    assert response.status_code == 200
    assert response.json()["response"] == "請重新提出刪除要求。"
    assert client.get(f"/tasks/{task['id']}").status_code == 200


def test_gemini_tool_definitions_include_delete_task() -> None:
    names = [declaration.name for declaration in gemini_provider.GEMINI_TOOLS[0].function_declarations]

    assert "delete_task" in names


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("create_task", {"title": ""}),
        ("create_task", "not-an-object"),
        ("unknown_tool", {}),
    ],
)
def test_gemini_invalid_or_unknown_tool_arguments_are_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tool_name: str, arguments: object
) -> None:
    mock_client = mock_gemini(
        monkeypatch,
        [gemini_tool_calls((tool_name, arguments)), gemini_text("請提供有效資訊。")],
    )

    response = client.post("/chat", json={"message": "建立任務"})

    assert response.status_code == 200
    assert client.get("/tasks").json() == []
    response_part = mock_client.models.calls[1]["contents"][-1].parts[0].function_response
    assert response_part.response["ok"] is False


def test_gemini_tool_execution_failure_is_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_database_error(*args, **kwargs):
        raise RuntimeError("sensitive database details")

    monkeypatch.setattr(ai_service.task_service, "create_task", raise_database_error)
    mock_gemini(
        monkeypatch,
        [gemini_tool_calls(("create_task", {"title": "Task"}))],
    )

    response = client.post("/chat", json={"message": "建立任務"})

    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is temporarily unavailable"}
    assert client.get("/tasks").json() == []


def test_gemini_conversation_history_is_reused_for_next_turn(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_gemini(
        monkeypatch,
        [
            gemini_tool_calls(("create_task", {"title": "Interview prep"})),
            gemini_text("已建立任務。"),
            gemini_tool_calls(("update_task", {"task_id": 1, "priority": "high"})),
            gemini_text("已更新任務。"),
        ],
    )

    first = client.post("/chat", json={"conversation_id": "gemini-demo", "message": "建立任務"})
    second = client.post("/chat", json={"conversation_id": "gemini-demo", "message": "改成高優先度"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert client.get("/tasks/1").json()["priority"] == "high"
    assert len(mock_client.models.calls[2]["contents"]) > 1
    state = get_persisted_conversation(client, "gemini-demo")
    assert state is not None
    assert state.provider == "gemini"
    assert state.last_response_id is None


def test_gemini_history_keeps_only_recent_complete_turns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "gemini_history_turns", 2)
    mock_client = mock_gemini(
        monkeypatch,
        [gemini_text(f"回覆 {index}") for index in range(1, 6)],
    )

    for index in range(1, 6):
        response = client.post(
            "/chat",
            json={"conversation_id": "trimmed", "message": f"訊息 {index}"},
        )
        assert response.status_code == 200

    fifth_request_texts = content_texts(mock_client.models.calls[4]["contents"])
    assert fifth_request_texts == ["訊息 3", "回覆 3", "訊息 4", "回覆 4", "訊息 5"]


def test_gemini_history_never_splits_multiple_function_call_responses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "gemini_history_turns", 1)
    mock_client = mock_gemini(
        monkeypatch,
        [
            gemini_tool_calls(
                ("create_task", {"title": "First"}),
                ("create_task", {"title": "Second"}),
            ),
            gemini_text("已建立兩項任務。"),
            gemini_text("第二輪回覆。"),
            gemini_text("第三輪回覆。"),
        ],
    )

    client.post("/chat", json={"conversation_id": "tools", "message": "建立兩項任務"})
    client.post("/chat", json={"conversation_id": "tools", "message": "第二輪"})
    response = client.post("/chat", json={"conversation_id": "tools", "message": "第三輪"})

    assert response.status_code == 200
    first_turn_function_call_content = mock_client.models.calls[1]["contents"][-1]
    function_responses = [
        part.function_response
        for part in first_turn_function_call_content.parts
        if part.function_response is not None
    ]
    assert [response.name for response in function_responses] == ["create_task", "create_task"]
    third_request_texts = content_texts(mock_client.models.calls[3]["contents"])
    assert third_request_texts == ["第二輪", "第二輪回覆。", "第三輪"]
    assert all(
        part.function_call is None and part.function_response is None
        for content in mock_client.models.calls[3]["contents"]
        for part in (content.parts or [])
    )


def test_gemini_failure_does_not_mutate_saved_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_gemini(monkeypatch, [gemini_text("第一輪回覆。"), RuntimeError("network failure")])
    first = client.post("/chat", json={"conversation_id": "stable", "message": "第一輪"})
    state_before = get_persisted_conversation(client, "stable")
    assert first.status_code == 200
    assert state_before is not None
    history_before = [content.model_dump() for content in state_before.provider_state]

    second = client.post("/chat", json={"conversation_id": "stable", "message": "失敗輪"})

    state_after = get_persisted_conversation(client, "stable")
    assert second.status_code == 503
    assert state_after == state_before
    assert state_after.turn_count == 1
    assert [content.model_dump() for content in state_after.provider_state] == history_before


def test_gemini_conversations_are_isolated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = mock_gemini(
        monkeypatch,
        [gemini_text("A"), gemini_text("B"), gemini_text("A2")],
    )

    client.post("/chat", json={"conversation_id": "gemini-a", "message": "A"})
    client.post("/chat", json={"conversation_id": "gemini-b", "message": "B"})
    response = client.post("/chat", json={"conversation_id": "gemini-a", "message": "A2"})

    assert response.status_code == 200
    assert len(mock_client.models.calls[2]["contents"]) > len(mock_client.models.calls[1]["contents"])


def test_conversation_provider_mismatch_returns_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_gemini(monkeypatch, [gemini_text("Gemini first turn")])
    client.post("/chat", json={"conversation_id": "shared", "message": "第一輪"})
    monkeypatch.setattr(settings, "ai_provider", "openai")

    response = client.post("/chat", json={"conversation_id": "shared", "message": "第二輪"})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Conversation provider does not match. Start a new conversation_id."
    }


@pytest.mark.parametrize(
    "history_payload",
    [
        {"schema_version": 1, "contents": [{"role": "user"}]},
        {"schema_version": 2, "contents": []},
        {
            "schema_version": 1,
            "contents": [
                {"role": "user", "parts": [{"text": "list tasks"}]},
                {
                    "role": "model",
                    "parts": [
                        {"function_call": {"name": "list_tasks", "args": {}}}
                    ],
                },
            ],
        },
    ],
)
def test_invalid_persisted_gemini_history_returns_conflict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, history_payload: dict
) -> None:
    with client.conversation_session_factory() as db:
        db.add(
                Conversation(
                    user_id=1,
                    conversation_id="corrupt-gemini-history",
                provider="gemini",
                gemini_history_json=history_payload,
                turn_count=1,
            )
        )
        db.commit()
    monkeypatch.setattr(settings, "ai_provider", "gemini")

    response = client.post(
        "/chat",
        json={"conversation_id": "corrupt-gemini-history", "message": "繼續"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Conversation context is unavailable. Start a new conversation_id."
    }


def test_gemini_api_failure_logs_safe_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    mock_gemini(monkeypatch, [RuntimeError("sensitive Gemini failure")])

    with caplog.at_level(logging.ERROR, logger="ai_task_assistant.services.gemini_provider"):
        response = client.post("/chat", json={"message": "private user message"})

    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is temporarily unavailable"}
    assert "stage=initial_response exception=RuntimeError status_code=none request_id=none" in caplog.text
    assert "sensitive Gemini failure" not in caplog.text
    assert "private user message" not in caplog.text


def test_gemini_api_failure_logs_status_and_request_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = RuntimeError("sensitive Gemini failure")
    error.status_code = 429  # type: ignore[attr-defined]
    error.request_id = "gemini-request-123"  # type: ignore[attr-defined]
    mock_gemini(monkeypatch, [error])

    with caplog.at_level(logging.ERROR, logger="ai_task_assistant.services.gemini_provider"):
        response = client.post("/chat", json={"message": "private user message"})

    assert response.status_code == 503
    assert "status_code=429" in caplog.text
    assert "request_id=gemini-request-123" in caplog.text
    assert "sensitive Gemini failure" not in caplog.text
