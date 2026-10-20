from collections.abc import Generator
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import get_db
from ai_task_assistant.main import app
from ai_task_assistant.models.task import Task


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

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
    Base.metadata.drop_all(bind=test_engine)


def create_task(client: TestClient) -> dict:
    response = client.post(
        "/tasks",
        json={
            "title": "Prepare project demo",
            "description": "Record a short walkthrough.",
            "due_date": "2026-10-01T09:00:00Z",
            "priority": "high",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_create_task(client: TestClient) -> None:
    task = create_task(client)

    assert task["id"] == 1
    assert task["title"] == "Prepare project demo"
    assert task["priority"] == "high"
    assert task["status"] == "pending"


def test_due_date_is_returned_in_asia_taipei(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={"title": "Timezone task", "due_date": "2026-10-01T15:00:00+08:00"},
    )

    assert response.status_code == 201
    assert response.json()["due_date"] == "2026-10-01T15:00:00+08:00"
    assert client.get(f"/tasks/{response.json()['id']}").json()["due_date"] == (
        "2026-10-01T15:00:00+08:00"
    )


def test_other_timezone_is_normalized_to_asia_taipei(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={"title": "Convert timezone", "due_date": "2026-10-01T15:00:00-04:00"},
    )

    assert response.status_code == 201
    assert response.json()["due_date"] == "2026-10-02T03:00:00+08:00"


def test_sqlite_storage_restores_asia_taipei_timezone() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    due_date = datetime(2026, 10, 1, 15, tzinfo=ZoneInfo("Asia/Taipei"))

    with Session(engine) as session:
        task = Task(title="Stored timezone task", due_date=due_date)
        session.add(task)
        session.commit()
        stored_value = session.execute(text("SELECT due_date FROM tasks")).scalar_one()
        session.expire(task)
        restored_task = session.get(Task, task.id)

    assert str(stored_value).startswith("2026-10-01 15:00:00")
    assert restored_task is not None
    assert restored_task.due_date == due_date
    assert restored_task.due_date.isoformat() == "2026-10-01T15:00:00+08:00"


def test_get_task(client: TestClient) -> None:
    created_task = create_task(client)

    response = client.get(f"/tasks/{created_task['id']}")

    assert response.status_code == 200
    assert response.json()["id"] == created_task["id"]


def test_list_tasks(client: TestClient) -> None:
    create_task(client)
    client.post("/tasks", json={"title": "Review pull request"})

    response = client.get("/tasks")

    assert response.status_code == 200
    assert [task["title"] for task in response.json()] == [
        "Prepare project demo",
        "Review pull request",
    ]


def test_update_task(client: TestClient) -> None:
    created_task = create_task(client)

    response = client.patch(
        f"/tasks/{created_task['id']}",
        json={"title": "Prepare final project demo", "status": "completed"},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Prepare final project demo"
    assert response.json()["status"] == "completed"
    assert response.json()["priority"] == "high"


def test_delete_task(client: TestClient) -> None:
    created_task = create_task(client)

    response = client.delete(f"/tasks/{created_task['id']}")

    assert response.status_code == 204
    assert client.get(f"/tasks/{created_task['id']}").status_code == 404


def test_task_not_found(client: TestClient) -> None:
    response = client.get("/tasks/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"title": "   "},
        {"title": "Valid title", "due_date": "not-a-date"},
    ],
)
def test_invalid_task_input(client: TestClient, payload: dict) -> None:
    response = client.post("/tasks", json=payload)

    assert response.status_code == 422


def test_naive_due_date_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={"title": "Naive datetime", "due_date": "2026-10-01T15:00:00"},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "payload",
    [
        {"title": ""},
        {"title": " "},
        {"title": "x" * 256},
        {"title": "Valid title", "due_date": "invalid-date"},
        {"title": "Valid title", "due_date": "2026-10-01T15:00:00"},
    ],
)
def test_invalid_task_payloads_return_validation_errors(client: TestClient, payload: dict) -> None:
    response = client.post("/tasks", json=payload)

    assert response.status_code == 422


def test_update_nonexistent_task_returns_standard_not_found(client: TestClient) -> None:
    response = client.patch("/tasks/999", json={"title": "Updated"})

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


def test_delete_nonexistent_task_returns_standard_not_found(client: TestClient) -> None:
    response = client.delete("/tasks/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


@pytest.mark.parametrize("path", ["/tasks/0", "/tasks/-1", "/tasks/not-a-number"])
def test_invalid_task_ids_return_validation_errors(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 422


@pytest.mark.parametrize(
    "field, value",
    [("priority", "urgent"), ("status", "in_progress")],
)
def test_invalid_priority_or_status(client: TestClient, field: str, value: str) -> None:
    response = client.post("/tasks", json={"title": "Valid title", field: value})

    assert response.status_code == 422
