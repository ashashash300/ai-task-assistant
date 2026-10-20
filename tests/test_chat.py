import json
from collections.abc import Generator
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import Request, Response
from openai import BadRequestError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import get_db
from ai_task_assistant.main import app
from ai_task_assistant.services import ai_service
from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.services import conversation_service


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

    conversation_service.clear_conversations()
    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    conversation_service.clear_conversations()
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
    monkeypatch.setattr(ai_service, "get_openai_client", lambda: mock_client)
    return mock_client


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


def test_llm_api_failure_is_safe(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_llm(monkeypatch, [RuntimeError("network failure")])

    response = client.post("/chat", json={"message": "建立任務"})

    assert response.status_code == 503
    assert response.json() == {"detail": "AI service is temporarily unavailable"}


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
    state = conversation_service.get_conversation("demo-001")
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
    assert conversation_service.get_conversation("demo-001").last_response_id == "resp_second"
    assert conversation_service.get_conversation("demo-001").turn_count == 2


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
    assert conversation_service.get_conversation("demo-001").last_response_id == "resp_final"


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
    assert conversation_service.get_conversation("service-failure") is None


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
    assert conversation_service.get_conversation("demo-001") is None


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

    state_before = conversation_service.get_conversation("limited")
    response = client.post("/chat", json={"conversation_id": "limited", "message": "第十一輪"})

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Conversation turn limit reached. Start a new conversation_id."
    }
    assert len(mock_client.responses.calls) == conversation_service.MAX_TURNS_PER_CONVERSATION
    assert conversation_service.get_conversation("limited") == state_before


def test_conversation_count_limit_rejects_new_conversation_without_calling_llm(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(conversation_service.MAX_CONVERSATIONS):
        conversation_service.save_response(f"conversation-{index}", f"resp-{index}")
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
            conversation_service.get_conversation(f"conversation-{index}")
            for index in range(conversation_service.MAX_CONVERSATIONS)
        ]
    ) == conversation_service.MAX_CONVERSATIONS


def test_llm_failure_does_not_create_conversation_state(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_llm(monkeypatch, [RuntimeError("network failure")])

    response = client.post(
        "/chat", json={"conversation_id": "failed", "message": "建立任務"}
    )

    assert response.status_code == 503
    assert conversation_service.get_conversation("failed") is None


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
    assert conversation_service.get_conversation("failed-tool") is None


def test_stale_previous_response_id_is_not_overwritten(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation_service.save_response("stale", "resp_stale")
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
    assert conversation_service.get_conversation("stale").last_response_id == "resp_stale"


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
    assert conversation_service.get_conversation("interview").last_response_id == "resp_turn_two"


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
