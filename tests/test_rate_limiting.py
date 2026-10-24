from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.api.dependencies.auth import require_csrf_protection, require_current_user
from ai_task_assistant.core.config import settings
from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import get_db
import ai_task_assistant.api.routes.auth as auth_route
import ai_task_assistant.main as main_module
from ai_task_assistant.main import app
from ai_task_assistant.models.user import User
from ai_task_assistant.services.rate_limit_service import clear_rate_limits


ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)

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
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        main_module.engine = original_engine
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


def csrf_headers(client: TestClient) -> dict[str, str]:
    response = client.get("/auth/csrf", headers={"Origin": ORIGIN})
    assert response.status_code == 200
    return {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


def test_login_ip_limit_runs_before_password_verification(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_ip_limit", 1)
    calls = 0

    def fake_authenticate(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(auth_route, "authenticate_user", fake_authenticate)
    headers = csrf_headers(client)
    first = client.post("/auth/login", headers=headers, json={"email": "user@example.com", "password": "long-safe-password"})
    second = client.post("/auth/login", headers=headers, json={"email": "user@example.com", "password": "long-safe-password"})

    assert first.status_code == 401
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert calls == 1


def test_login_email_limit_and_untrusted_forwarded_for_cannot_bypass(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "login_ip_limit", 1)
    monkeypatch.setattr(settings, "login_email_limit", 10)
    headers = csrf_headers(client)
    payload = {"email": "target@example.com", "password": "long-safe-password"}
    assert client.post("/auth/login", headers={**headers, "X-Forwarded-For": "198.51.100.1"}, json=payload).status_code == 401
    response = client.post("/auth/login", headers={**headers, "X-Forwarded-For": "203.0.113.2"}, json=payload)
    assert response.status_code == 429
    assert response.json() == {"detail": "Too many requests"}


def test_register_and_csrf_ip_limits_return_429_before_expensive_work(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "register_ip_limit", 1)
    calls = 0

    def fake_register(_db, *, email: str, password: str) -> User:
        nonlocal calls
        calls += 1
        return User(id=calls, email=email, password_hash=password)

    monkeypatch.setattr(auth_route, "register_user", fake_register)
    headers = csrf_headers(client)
    first = client.post("/auth/register", headers=headers, json={"email": "first@example.com", "password": "long-safe-password"})
    second = client.post("/auth/register", headers=headers, json={"email": "second@example.com", "password": "long-safe-password"})
    assert first.status_code == 201
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "3600"
    assert calls == 1

    clear_rate_limits()
    monkeypatch.setattr(settings, "csrf_ip_limit", 1)
    first_csrf = client.get("/auth/csrf", headers={"Origin": ORIGIN})
    second_csrf = client.get("/auth/csrf", headers={"Origin": ORIGIN})
    assert first_csrf.status_code == 200
    assert second_csrf.status_code == 429


def test_auth_rate_limiter_does_not_expose_email_or_ip(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "login_ip_limit", 1)
    headers = csrf_headers(client)
    client.post("/auth/login", headers=headers, json={"email": "private@example.com", "password": "long-safe-password"})
    response = client.post("/auth/login", headers=headers, json={"email": "private@example.com", "password": "long-safe-password"})
    assert response.status_code == 429
    assert "private@example.com" not in response.text
    assert "testclient" not in response.text
