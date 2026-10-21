import json
import logging
from collections.abc import Generator
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import Request, Response
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    PermissionDeniedError,
    RateLimitError,
)
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import get_db
import ai_task_assistant.api.routes.chat as chat_route
import ai_task_assistant.main as main_module
from ai_task_assistant.main import app
from ai_task_assistant.services import ai_service
from ai_task_assistant.core.config import settings
from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.services import conversation_service
from ai_task_assistant.services import openai_provider


class MockResponses:
    def __init__(self, responses: list[object]) -> None:
        self.responses_to_return = responses
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses_to_return.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class MockOpenAIClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = MockResponses(responses)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_session = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=test_engine)

    def override_get_db() -> Generator[Session, None, None]:
        db = test_session()
        try:
            yield db
        finally:
            db.close()

    conversation_service.clear_pending_deletions()
    app.dependency_overrides[get_db] = override_get_db
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


def tool_call(name: str, arguments: dict, response_id: str = "resp_tool") -> object:
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                name=name,
                arguments=json.dumps(arguments),
                call_id="call_123",
            )
        ],
        output_text="",
        id=response_id,
    )


def tool_call_with_raw_arguments(name: str, arguments: str) -> object:
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                name=name,
                arguments=arguments,
                call_id="call_raw",
            )
        ],
        output_text="",
        id="resp_raw_tool",
    )


def final_response(text: str, response_id: str = "resp_final") -> object:
    return SimpleNamespace(output=[], output_text=text, id=response_id)


def mock_llm(monkeypatch: pytest.MonkeyPatch, responses: list[object]) -> MockOpenAIClient:
    mock_client = MockOpenAIClient(responses)
    monkeypatch.setattr(settings, "ai_provider", "openai")
    monkeypatch.setattr(openai_provider, "get_openai_client", lambda: mock_client)
    return mock_client


def get_persisted_conversation(client: TestClient, conversation_id: str):
    with client.conversation_session_factory() as db:
        return conversation_service.get_conversation(db, conversation_id)


def save_persisted_openai_conversation(
    client: TestClient, conversation_id: str, response_id: str
) -> None:
    with client.conversation_session_factory() as db:
        conversation_service.save_response(
            db,
            conversation_id,
            response_id,
            "openai",
            expected_state_version=None,
        )


def test_chat_request_validation(client: TestClient) -> None:
    response = client.post("/chat", json={"message": "   "})

    assert response.status_code == 422


def test_system_prompt_uses_asia_taipei_time(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = mock_llm(monkeypatch, [final_response("您好")])

    response = client.post("/chat", json={"message": "你好"})

    assert response.status_code == 200
    system_prompt = mock_client.responses.calls[0]["input"][0]["content"]
    assert "Asia/Taipei" in system_prompt
    assert "+08:00" in system_prompt
    assert "ask a clarifying question" in system_prompt


@pytest.mark.parametrize(
    "message",
    ["幫我安排一下", "明天", "明天下午", "把那個改一下", "asdfghjkl"],
)
def test_incomplete_or_ambiguous_messages_do_not_execute_tools(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    mock_client = mock_llm(monkeypatch, [final_response("請提供更多任務資訊。")])

    response = client.post("/chat", json={"message": message})

    assert response.status_code == 200
    assert response.json() == {"response": "請提供更多任務資訊。"}
    assert len(mock_client.responses.calls) == 1
    assert client.get("/tasks").json() == []


def test_create_task_through_chat(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_llm(
        monkeypatch,
        [
            tool_call("create_task", {"title": "Prepare AI interview", "priority": "high"}),
            final_response("已建立「Prepare AI interview」。"),
        ],
    )

    response = client.post("/chat", json={"message": "提醒我準備 AI 面試"})

    assert response.status_code == 200
    assert response.json() == {"response": "已建立「Prepare AI interview」。"}
    assert client.get("/tasks").json()[0]["title"] == "Prepare AI interview"


def test_list_tasks_through_chat(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/tasks", json={"title": "Existing task"})
    mock_client = mock_llm(
        monkeypatch,
        [tool_call("list_tasks", {}), final_response("你有 1 個待辦事項。")],
    )

    response = client.post("/chat", json={"message": "列出我的任務"})

    assert response.status_code == 200
    assert response.json()["response"] == "你有 1 個待辦事項。"
    tool_output = mock_client.responses.calls[1]["input"][-1]["output"]
    assert json.loads(tool_output)["tasks"][0]["title"] == "Existing task"


def test_update_task_through_chat(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = client.post("/tasks", json={"title": "Draft notes"}).json()["id"]
    mock_llm(
        monkeypatch,
        [
            tool_call("update_task", {"task_id": task_id, "priority": "high"}),
            final_response("已提高任務優先度。"),
        ],
    )

    response = client.post("/chat", json={"message": "把草稿筆記設為高優先度"})

    assert response.status_code == 200
    assert client.get(f"/tasks/{task_id}").json()["priority"] == "high"


def test_complete_task_through_chat(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    task_id = client.post("/tasks", json={"title": "Submit application"}).json()["id"]
    mock_llm(
        monkeypatch,
        [tool_call("complete_task", {"task_id": task_id}), final_response("任務已完成。")],
    )

    response = client.post("/chat", json={"message": "完成申請工作"})

    assert response.status_code == 200
    assert client.get(f"/tasks/{task_id}").json()["status"] == "completed"


def test_invalid_tool_arguments_are_safe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [tool_call("create_task", {"title": ""}), final_response("我需要有效的任務標題。")],
    )

    response = client.post("/chat", json={"message": "建立任務"})

    assert response.status_code == 200
    assert client.get("/tasks").json() == []
    assert json.loads(mock_client.responses.calls[1]["input"][-1]["output"]) == {
        "ok": False,
        "error": "Invalid tool arguments",
    }


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("create_task", {}),
        ("create_task", {"title": ["not a string"]}),
        ("create_task", {"title": "Task", "priority": "urgent"}),
        ("create_task", {"title": "Task", "due_date": "not-a-date"}),
        ("update_task", {"task_id": 1, "status": "in_progress"}),
        ("update_task", {"task_id": "one", "title": "Task"}),
    ],
)
def test_malformed_tool_arguments_are_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, name: str, arguments: dict
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [tool_call(name, arguments), final_response("請提供有效資訊。")],
    )

    response = client.post("/chat", json={"message": "處理任務"})

    assert response.status_code == 200
    assert client.get("/tasks").json() == []
    assert json.loads(mock_client.responses.calls[1]["input"][-1]["output"]) == {
        "ok": False,
        "error": "Invalid tool arguments",
    }


def test_complete_nonexistent_task_tool_is_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [tool_call("complete_task", {"task_id": 999}), final_response("找不到該任務。")],
    )

    response = client.post("/chat", json={"message": "完成編號 999 的任務"})

    assert response.status_code == 200
    assert json.loads(mock_client.responses.calls[1]["input"][-1]["output"]) == {
        "ok": False,
        "error": "Task not found",
    }


@pytest.mark.parametrize("raw_arguments", ["{not-json", "[]", "null"])
def test_non_object_or_invalid_json_tool_arguments_are_safe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, raw_arguments: str
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [tool_call_with_raw_arguments("create_task", raw_arguments), final_response("請提供有效資訊。")],
    )

    response = client.post("/chat", json={"message": "建立任務"})

    assert response.status_code == 200
    assert json.loads(mock_client.responses.calls[1]["input"][-1]["output"]) == {
        "ok": False,
        "error": "Invalid tool arguments",
    }


def test_naive_tool_due_date_is_rejected_safely(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [
            tool_call("create_task", {"title": "Naive due date", "due_date": "2026-10-01T15:00:00"}),
            final_response("請提供含時區的時間。"),
        ],
    )

    response = client.post("/chat", json={"message": "建立任務"})

    assert response.status_code == 200
    assert client.get("/tasks").json() == []
    assert json.loads(mock_client.responses.calls[1]["input"][-1]["output"]) == {
        "ok": False,
        "error": "Invalid tool arguments",
    }


@pytest.mark.parametrize(
    ("message", "requested_time"),
    [("明天下午三點提醒我", time(15, 0)), ("明天晚上八點提醒我", time(20, 0))],
)
def test_relative_time_tool_calls_use_asia_taipei(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    requested_time: time,
) -> None:
    tomorrow = current_taipei_time().date() + timedelta(days=1)
    due_date = datetime.combine(tomorrow, requested_time, tzinfo=ZoneInfo("Asia/Taipei"))
    mock_llm(
        monkeypatch,
        [
            tool_call("create_task", {"title": "Relative time", "due_date": due_date.isoformat()}),
            final_response("已建立提醒。"),
        ],
    )

    response = client.post("/chat", json={"message": message})

    assert response.status_code == 200
    assert client.get("/tasks").json()[0]["due_date"] == due_date.isoformat()


@pytest.mark.parametrize("message", ["明天", "後天", "下週五"])
def test_missing_relative_time_does_not_create_a_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    mock_client = mock_llm(monkeypatch, [final_response("請問您希望幾點提醒？")])

    response = client.post("/chat", json={"message": message})

    assert response.status_code == 200
    assert response.json() == {"response": "請問您希望幾點提醒？"}
    assert len(mock_client.responses.calls) == 1
    assert client.get("/tasks").json() == []


@pytest.mark.parametrize(
    ("filter_arguments", "expected_titles"),
    [
        ({"due_after": "2026-10-02T00:00:00+08:00"}, ["Later task"]),
        ({"due_before": "2026-10-02T00:00:00+08:00"}, ["Earlier task"]),
    ],
)
def test_chat_date_filtering_uses_taipei_storage_values(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    filter_arguments: dict,
    expected_titles: list[str],
) -> None:
    client.post(
        "/tasks",
        json={"title": "Earlier task", "due_date": "2026-10-01T15:00:00+08:00"},
    )
    client.post(
        "/tasks",
        json={"title": "Later task", "due_date": "2026-10-02T15:00:00+08:00"},
    )
    mock_client = mock_llm(
        monkeypatch,
        [tool_call("list_tasks", filter_arguments), final_response("已列出任務。")],
    )

    response = client.post("/chat", json={"message": "依日期列出任務"})

    assert response.status_code == 200
    tool_output = json.loads(mock_client.responses.calls[1]["input"][-1]["output"])
    assert [task["title"] for task in tool_output["tasks"]] == expected_titles


def test_task_not_found_is_safe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [tool_call("complete_task", {"task_id": 999}), final_response("找不到該任務。")],
    )

    response = client.post("/chat", json={"message": "完成編號 999 的任務"})

    assert response.status_code == 200
    assert json.loads(mock_client.responses.calls[1]["input"][-1]["output"]) == {
        "ok": False,
        "error": "Task not found",
    }


def test_openai_delete_tool_requires_confirmation_before_deleting(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = client.post("/tasks", json={"title": "Delete me"}).json()
    mock_llm(
        monkeypatch,
        [tool_call("delete_task", {"task_id": task["id"]}), final_response("請確認刪除。")],
    )

    requested = client.post(
        "/chat", json={"conversation_id": "delete-openai", "message": "刪除這個任務"}
    )

    assert requested.status_code == 200
    assert requested.json()["response"] == "請確認是否刪除「Delete me」？"
    assert client.get(f"/tasks/{task['id']}").status_code == 200
    pending = conversation_service.get_pending_deletion("delete-openai")
    assert pending is not None
    assert pending.task_id == task["id"]

    confirmed = client.post(
        "/chat", json={"conversation_id": "delete-openai", "message": "確認"}
    )

    assert confirmed.status_code == 200
    assert confirmed.json()["response"] == "已刪除「Delete me」。"
    assert client.get(f"/tasks/{task['id']}").status_code == 404
    assert conversation_service.get_pending_deletion("delete-openai") is None


def test_delete_confirmation_can_be_cancelled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    task = client.post("/tasks", json={"title": "Keep me"}).json()
    mock_llm(
        monkeypatch,
        [tool_call("delete_task", {"task_id": task["id"]}), final_response("請確認刪除。")],
    )
    client.post("/chat", json={"conversation_id": "delete-cancel", "message": "刪除任務"})

    cancelled = client.post(
        "/chat", json={"conversation_id": "delete-cancel", "message": "取消"}
    )

    assert cancelled.json()["response"] == "已取消刪除「Keep me」。"
    assert client.get(f"/tasks/{task['id']}").status_code == 200
    assert conversation_service.get_pending_deletion("delete-cancel") is None


def test_confirmation_without_pending_deletion_cannot_delete_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = client.post("/tasks", json={"title": "Safe task"}).json()
    mock_llm(monkeypatch, [final_response("沒有待確認的刪除。")])

    response = client.post(
        "/chat", json={"conversation_id": "no-pending", "message": "確認"}
    )

    assert response.status_code == 200
    assert client.get(f"/tasks/{task['id']}").status_code == 200


def test_pending_deletion_expires_after_application_restart(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = client.post("/tasks", json={"title": "Restart-safe task"}).json()
    mock_llm(
        monkeypatch,
        [
            tool_call("delete_task", {"task_id": task["id"]}),
            final_response("請確認刪除。", "resp_delete_request"),
            final_response("請重新提出刪除要求。", "resp_after_restart"),
        ],
    )
    client.post(
        "/chat", json={"conversation_id": "restart-delete", "message": "刪除任務"}
    )

    # Simulate a process restart: persisted conversation remains, pending authorization does not.
    conversation_service.clear_pending_deletions()
    response = client.post(
        "/chat", json={"conversation_id": "restart-delete", "message": "確認"}
    )

    assert response.status_code == 200
    assert client.get(f"/tasks/{task['id']}").status_code == 200
    assert conversation_service.get_pending_deletion("restart-delete") is None


def test_pending_deletions_are_isolated_by_conversation_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = client.post("/tasks", json={"title": "Isolated task"}).json()
    mock_llm(
        monkeypatch,
        [
            tool_call("delete_task", {"task_id": task["id"]}),
            final_response("請確認刪除。"),
            final_response("沒有待確認的刪除。"),
        ],
    )
    client.post("/chat", json={"conversation_id": "delete-a", "message": "刪除任務"})

    wrong_conversation = client.post(
        "/chat", json={"conversation_id": "delete-b", "message": "確認"}
    )
    assert wrong_conversation.status_code == 200
    assert client.get(f"/tasks/{task['id']}").status_code == 200

    right_conversation = client.post(
        "/chat", json={"conversation_id": "delete-a", "message": "確認"}
    )

    assert client.get(f"/tasks/{task['id']}").status_code == 404
    assert right_conversation.json()["response"] == "已刪除「Isolated task」。"


def test_new_delete_request_replaces_earlier_pending_deletion(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_task = client.post("/tasks", json={"title": "First task"}).json()
    second_task = client.post("/tasks", json={"title": "Second task"}).json()
    mock_llm(
        monkeypatch,
        [
            tool_call("delete_task", {"task_id": first_task["id"]}),
            final_response("請確認刪除。"),
            tool_call("delete_task", {"task_id": second_task["id"]}),
            final_response("請確認刪除。"),
        ],
    )

    client.post("/chat", json={"conversation_id": "replace-delete", "message": "刪除第一筆"})
    requested = client.post(
        "/chat", json={"conversation_id": "replace-delete", "message": "刪除第二筆"}
    )
    confirmed = client.post(
        "/chat", json={"conversation_id": "replace-delete", "message": "確認"}
    )

    assert requested.json()["response"] == "請確認是否刪除「Second task」？"
    assert confirmed.status_code == 200
    assert client.get(f"/tasks/{first_task['id']}").status_code == 200
    assert client.get(f"/tasks/{second_task['id']}").status_code == 404


def test_expired_delete_confirmation_cannot_delete_task(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = client.post("/tasks", json={"title": "Expired request"}).json()
    mock_llm(
        monkeypatch,
        [
            tool_call("delete_task", {"task_id": task["id"]}),
            final_response("請確認刪除。"),
            final_response("沒有待確認的刪除。"),
        ],
    )
    client.post("/chat", json={"conversation_id": "expired-delete", "message": "刪除任務"})
    pending = conversation_service.get_pending_deletion("expired-delete")
    assert pending is not None
    pending.expires_at = current_taipei_time() - timedelta(seconds=1)

    response = client.post(
        "/chat", json={"conversation_id": "expired-delete", "message": "確認"}
    )

    assert response.status_code == 200
    assert client.get(f"/tasks/{task['id']}").status_code == 200
    assert conversation_service.get_pending_deletion("expired-delete") is None


def test_delete_tool_handles_nonexistent_task_and_cannot_be_reused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = client.post("/tasks", json={"title": "Delete once"}).json()
    mock_client = mock_llm(
        monkeypatch,
        [
            tool_call("delete_task", {"task_id": 999}),
            final_response("找不到任務。"),
            tool_call("delete_task", {"task_id": task["id"]}),
            final_response("請確認刪除。"),
            final_response("沒有待確認的刪除。"),
        ],
    )

    missing = client.post(
        "/chat", json={"conversation_id": "missing-delete", "message": "刪除 999"}
    )
    requested = client.post(
        "/chat", json={"conversation_id": "delete-once", "message": "刪除任務"}
    )
    first_confirmation = client.post(
        "/chat", json={"conversation_id": "delete-once", "message": "確認"}
    )
    second_confirmation = client.post(
        "/chat", json={"conversation_id": "delete-once", "message": "確認"}
    )

    assert missing.status_code == 200
    assert conversation_service.get_pending_deletion("missing-delete") is None
    assert requested.json()["response"] == "請確認是否刪除「Delete once」？"
    assert first_confirmation.status_code == 200
    assert second_confirmation.status_code == 200
    assert client.get(f"/tasks/{task['id']}").status_code == 404
    assert len(mock_client.responses.calls) == 5


def test_llm_api_failure_is_safe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_llm(monkeypatch, [RuntimeError("network failure")])

    response = client.post("/chat", json={"message": "建立任務"})

    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is temporarily unavailable"}


def test_missing_gemini_key_returns_safe_error_without_openai_fallback(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_if_openai_is_called():
        pytest.fail("OpenAI must not be called when Gemini is selected")

    monkeypatch.setattr(settings, "ai_provider", "gemini")
    monkeypatch.setattr(settings, "gemini_api_key", None)
    monkeypatch.setattr(openai_provider, "get_openai_client", fail_if_openai_is_called)

    response = client.post("/chat", json={"message": "建立任務"})

    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is not configured"}


def openai_status_error(error_class: type[Exception], status_code: int) -> Exception:
    request = Request("POST", "https://api.openai.com/v1/responses")
    response = Response(
        status_code,
        request=request,
        headers={"x-request-id": "req_diagnostic_123"},
    )
    return error_class("sensitive error content must not be logged", response=response, body=None)


@pytest.mark.parametrize(
    ("error", "exception_name", "status_code", "request_id"),
    [
        (openai_status_error(AuthenticationError, 401), "AuthenticationError", "401", "req_diagnostic_123"),
        (openai_status_error(PermissionDeniedError, 403), "PermissionDeniedError", "403", "req_diagnostic_123"),
        (openai_status_error(RateLimitError, 429), "RateLimitError", "429", "req_diagnostic_123"),
        (openai_status_error(BadRequestError, 400), "BadRequestError", "400", "req_diagnostic_123"),
        (
            APIConnectionError(
                message="sensitive connection error content must not be logged",
                request=Request("POST", "https://api.openai.com/v1/responses"),
            ),
            "APIConnectionError",
            "none",
            "none",
        ),
        (
            APITimeoutError(Request("POST", "https://api.openai.com/v1/responses")),
            "APITimeoutError",
            "none",
            "none",
        ),
    ],
)
def test_openai_failures_log_safe_diagnostic_metadata(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    exception_name: str,
    status_code: str,
    request_id: str,
) -> None:
    mock_llm(monkeypatch, [error])

    with caplog.at_level(logging.ERROR, logger="ai_task_assistant.services.openai_provider"):
        response = client.post("/chat", json={"message": "private user message"})

    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is temporarily unavailable"}
    assert f"stage=initial_response exception={exception_name}" in caplog.text
    assert f"status_code={status_code}" in caplog.text
    assert f"request_id={request_id}" in caplog.text
    assert "sensitive error content" not in caplog.text
    assert "private user message" not in caplog.text


def test_tool_execution_failure_logs_safe_diagnostic_metadata(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def raise_database_error(*args, **kwargs):
        raise RuntimeError("sensitive database details must not be logged")

    monkeypatch.setattr(ai_service.task_service, "create_task", raise_database_error)
    mock_llm(monkeypatch, [tool_call("create_task", {"title": "Private task"})])

    with caplog.at_level(logging.ERROR, logger="ai_task_assistant.services.openai_provider"):
        response = client.post("/chat", json={"message": "private user message"})

    assert response.status_code == 503
    assert "stage=tool_execution exception=RuntimeError status_code=none request_id=none" in caplog.text
    assert "sensitive database details" not in caplog.text
    assert "private user message" not in caplog.text


def test_followup_response_failure_logs_its_stage(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    error = openai_status_error(RateLimitError, 429)
    mock_llm(
        monkeypatch,
        [tool_call("list_tasks", {}), error],
    )

    with caplog.at_level(logging.ERROR, logger="ai_task_assistant.services.openai_provider"):
        response = client.post("/chat", json={"message": "private user message"})

    assert response.status_code == 503
    assert "stage=followup_response exception=RateLimitError" in caplog.text
    assert "request_id=req_diagnostic_123" in caplog.text
    assert "sensitive error content" not in caplog.text
    assert "private user message" not in caplog.text


def test_conversation_first_turn_saves_final_response_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_llm(monkeypatch, [final_response("已建立對話。", "resp_first_final")])

    response = client.post(
        "/chat", json={"conversation_id": "demo-001", "message": "你好"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "conversation_id": "demo-001",
        "response": "已建立對話。",
    }
    state = get_persisted_conversation(client, "demo-001")
    assert state is not None
    assert state.last_response_id == "resp_first_final"
    assert state.turn_count == 1
    assert state.updated_at.tzinfo == ZoneInfo("Asia/Taipei")


def test_same_conversation_sends_previous_response_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [
            final_response("第一輪", "resp_first"),
            final_response("第二輪", "resp_second"),
        ],
    )

    client.post("/chat", json={"conversation_id": "demo-001", "message": "第一句"})
    response = client.post("/chat", json={"conversation_id": "demo-001", "message": "第二句"})

    assert response.status_code == 200
    assert mock_client.responses.calls[0].get("previous_response_id") is None
    assert mock_client.responses.calls[1]["previous_response_id"] == "resp_first"
    assert get_persisted_conversation(client, "demo-001").last_response_id == "resp_second"
    assert get_persisted_conversation(client, "demo-001").turn_count == 2


def test_tool_turn_stores_final_response_id_not_intermediate_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [
            tool_call("create_task", {"title": "Interview prep"}, "resp_intermediate"),
            final_response("已建立任務。", "resp_final"),
        ],
    )

    response = client.post(
        "/chat", json={"conversation_id": "demo-001", "message": "建立任務"}
    )

    assert response.status_code == 200
    assert mock_client.responses.calls[1]["previous_response_id"] == "resp_intermediate"
    assert get_persisted_conversation(client, "demo-001").last_response_id == "resp_final"


def test_conversation_ids_are_isolated(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [
            final_response("A 第一輪", "resp_a"),
            final_response("B 第一輪", "resp_b"),
            final_response("A 第二輪", "resp_a_next"),
        ],
    )

    client.post("/chat", json={"conversation_id": "demo-a", "message": "A"})
    client.post("/chat", json={"conversation_id": "demo-b", "message": "B"})
    response = client.post("/chat", json={"conversation_id": "demo-a", "message": "A again"})

    assert response.status_code == 200
    assert mock_client.responses.calls[2]["previous_response_id"] == "resp_a"
    assert mock_client.responses.calls[2]["previous_response_id"] != "resp_b"


def test_conversation_b_does_not_receive_conversation_a_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [
            tool_call("create_task", {"title": "Conversation A task"}, "resp_a_tool"),
            final_response("A 已建立任務。", "resp_a_final"),
            final_response("請問您想查詢哪些任務？", "resp_b_final"),
        ],
    )

    first = client.post(
        "/chat", json={"conversation_id": "conversation-a", "message": "建立任務"}
    )
    second = client.post(
        "/chat", json={"conversation_id": "conversation-b", "message": "我的任務呢？"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert "previous_response_id" not in mock_client.responses.calls[2]
    assert second.json()["response"] == "請問您想查詢哪些任務？"


def test_task_service_failure_does_not_update_conversation_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def raise_database_error(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(ai_service.task_service, "create_task", raise_database_error)
    mock_llm(
        monkeypatch,
        [tool_call("create_task", {"title": "Task with service failure"}, "resp_tool")],
    )

    response = client.post(
        "/chat", json={"conversation_id": "service-failure", "message": "建立任務"}
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is temporarily unavailable"}
    assert get_persisted_conversation(client, "service-failure") is None


def test_omitted_conversation_id_remains_stateless(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [final_response("第一輪", "resp_one"), final_response("第二輪", "resp_two")],
    )

    first = client.post("/chat", json={"message": "第一句"})
    second = client.post("/chat", json={"message": "第二句"})

    assert first.json() == {"response": "第一輪"}
    assert second.json() == {"response": "第二輪"}
    assert "previous_response_id" not in mock_client.responses.calls[0]
    assert "previous_response_id" not in mock_client.responses.calls[1]
    assert get_persisted_conversation(client, "demo-001") is None


@pytest.mark.parametrize("conversation_id", ["", "   ", "demo id", "demo!001", "x" * 65])
def test_invalid_conversation_id_is_rejected(client: TestClient, conversation_id: str) -> None:
    response = client.post(
        "/chat", json={"conversation_id": conversation_id, "message": "你好"}
    )

    assert response.status_code == 422


def test_conversation_turn_limit_rejects_next_request_without_calling_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = [final_response("完成", f"resp_{index}") for index in range(10)]
    mock_client = mock_llm(monkeypatch, responses)

    for _ in range(conversation_service.MAX_TURNS_PER_CONVERSATION):
        response = client.post(
            "/chat", json={"conversation_id": "limited", "message": "繼續"}
        )
        assert response.status_code == 200

    state_before = get_persisted_conversation(client, "limited")
    response = client.post("/chat", json={"conversation_id": "limited", "message": "第十一輪"})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Conversation turn limit reached. Start a new conversation_id."
    }
    assert len(mock_client.responses.calls) == conversation_service.MAX_TURNS_PER_CONVERSATION
    assert get_persisted_conversation(client, "limited") == state_before


def test_conversation_count_limit_rejects_new_conversation_without_calling_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(conversation_service.MAX_CONVERSATIONS):
        save_persisted_openai_conversation(client, f"conversation-{index}", f"resp-{index}")
    mock_client = mock_llm(monkeypatch, [])

    response = client.post(
        "/chat", json={"conversation_id": "one-too-many", "message": "你好"}
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Conversation limit reached. Use an existing conversation_id or restart the application."
    }
    assert mock_client.responses.calls == []
    assert len(
        [
            get_persisted_conversation(client, f"conversation-{index}")
            for index in range(conversation_service.MAX_CONVERSATIONS)
        ]
    ) == conversation_service.MAX_CONVERSATIONS


@pytest.mark.parametrize(
    "message",
    [
        "Conversation was updated by another request. Retry the request.",
        "Conversation was created by another request. Retry the request.",
    ],
)
def test_conversation_save_conflicts_return_http_409(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    mock_llm(monkeypatch, [final_response("完成")])

    def raise_conflict(*args, **kwargs):
        raise conversation_service.ConversationConflictError(message)

    monkeypatch.setattr(chat_route, "save_response", raise_conflict)

    response = client.post(
        "/chat", json={"conversation_id": "conflicted", "message": "繼續"}
    )

    assert response.status_code == 409
    assert response.json() == {"detail": message}


def test_llm_failure_does_not_create_conversation_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_llm(monkeypatch, [RuntimeError("network failure")])

    response = client.post(
        "/chat", json={"conversation_id": "failed", "message": "建立任務"}
    )

    assert response.status_code == 503
    assert get_persisted_conversation(client, "failed") is None


def test_tool_final_response_failure_does_not_create_conversation_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_llm(
        monkeypatch,
        [
            tool_call("create_task", {"title": "Task before failure"}, "resp_intermediate"),
            RuntimeError("network failure"),
        ],
    )

    response = client.post(
        "/chat", json={"conversation_id": "failed-tool", "message": "建立任務"}
    )

    assert response.status_code == 503
    assert get_persisted_conversation(client, "failed-tool") is None


def test_stale_previous_response_id_is_not_overwritten(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_persisted_openai_conversation(client, "stale", "resp_stale")
    stale_error = BadRequestError(
        "previous response not found",
        response=Response(400, request=Request("POST", "https://api.openai.com/v1/responses")),
        body=None,
    )
    mock_client = mock_llm(monkeypatch, [stale_error])

    response = client.post("/chat", json={"conversation_id": "stale", "message": "繼續"})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Conversation context is unavailable. Start a new conversation_id."
    }
    assert mock_client.responses.calls[0]["previous_response_id"] == "resp_stale"
    assert get_persisted_conversation(client, "stale").last_response_id == "resp_stale"


def test_multi_turn_task_update_uses_prior_tool_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_client = mock_llm(
        monkeypatch,
        [
            tool_call(
                "create_task",
                {
                    "title": "Prepare AI interview",
                    "due_date": "2026-10-01T15:00:00+08:00",
                },
                "resp_create_tool",
            ),
            final_response("已建立任務。", "resp_turn_one"),
            tool_call(
                "update_task",
                {"task_id": 1, "due_date": "2026-10-01T20:00:00+08:00"},
                "resp_update_tool",
            ),
            final_response("已修改為晚上八點。", "resp_turn_two"),
        ],
    )

    first = client.post(
        "/chat",
        json={"conversation_id": "interview", "message": "明天下午三點提醒我準備 AI 面試"},
    )
    second = client.post(
        "/chat", json={"conversation_id": "interview", "message": "改成晚上八點"}
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert mock_client.responses.calls[2]["previous_response_id"] == "resp_turn_one"
    assert client.get("/tasks/1").json()["due_date"] == "2026-10-01T20:00:00+08:00"
    assert get_persisted_conversation(client, "interview").last_response_id == "resp_turn_two"


def test_ambiguous_task_reference_requests_clarification(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.post("/tasks", json={"title": "First interview", "due_date": "2026-10-01T15:00:00+08:00"})
    client.post("/tasks", json={"title": "Second interview", "due_date": "2026-10-02T15:00:00+08:00"})
    mock_client = mock_llm(monkeypatch, [final_response("請問您要修改哪一個任務？")])

    response = client.post("/chat", json={"conversation_id": "ambiguous", "message": "改成晚上八點"})

    assert response.status_code == 200
    assert response.json()["response"] == "請問您要修改哪一個任務？"
    assert len(mock_client.responses.calls) == 1
    assert [task["due_date"] for task in client.get("/tasks").json()] == [
        "2026-10-01T15:00:00+08:00",
        "2026-10-02T15:00:00+08:00",
    ]
