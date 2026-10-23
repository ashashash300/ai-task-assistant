from collections.abc import Generator
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.api.dependencies.auth import (
    CSRF_COOKIE_NAME,
    CSRF_ERROR_CODE,
    SESSION_COOKIE_NAME,
)
from ai_task_assistant.core.config import settings
from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import get_db
import ai_task_assistant.main as main_module
from ai_task_assistant.main import app
from ai_task_assistant.models.session import UserSession
from ai_task_assistant.models.user import User


ALLOWED_ORIGIN = "http://127.0.0.1:5173"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Generator[TestClient, None, None]:
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

    monkeypatch.setattr(settings, "session_cookie_secure", False)
    app.dependency_overrides[get_db] = override_get_db
    original_engine = main_module.engine
    main_module.engine = test_engine
    try:
        with TestClient(app) as test_client:
            test_client.auth_session_factory = test_session
            yield test_client
    finally:
        main_module.engine = original_engine
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=test_engine)


def csrf_headers(client: TestClient, *, origin: str = ALLOWED_ORIGIN) -> dict[str, str]:
    response = client.get("/auth/csrf", headers={"Origin": origin})
    assert response.status_code == 200
    return {"Origin": origin, "X-CSRF-Token": response.json()["csrf_token"]}


def register(client: TestClient, *, email: str = "user@example.com", password: str = "long-safe-password"):
    return client.post(
        "/auth/register",
        headers=csrf_headers(client),
        json={"email": email, "password": password},
    )


def login(client: TestClient, *, email: str = "user@example.com", password: str = "long-safe-password"):
    return client.post(
        "/auth/login",
        headers=csrf_headers(client),
        json={"email": email, "password": password},
    )


def test_register_normalizes_email_hashes_password_and_hides_sensitive_fields(client: TestClient) -> None:
    response = register(client, email="  User@Example.COM ")

    assert response.status_code == 201
    assert response.json() == {"id": 1, "email": "user@example.com"}
    assert "password_hash" not in response.text
    assert "token_hash" not in response.text
    with client.auth_session_factory() as db:
        user = db.get(User, 1)
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert user.password_hash != "long-safe-password"


def test_duplicate_email_returns_a_consistent_safe_error(client: TestClient) -> None:
    assert register(client).status_code == 201

    response = register(client, email=" USER@EXAMPLE.COM ")

    assert response.status_code == 409
    assert response.json() == {"detail": "Unable to create account with this email"}


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "not-an-email", "password": "long-safe-password"},
        {"email": "user@example.com", "password": "short"},
        {"email": "user@example.com", "password": "x" * 129},
    ],
)
def test_registration_validates_email_and_bounded_password(client: TestClient, payload: dict[str, str]) -> None:
    response = client.post("/auth/register", headers=csrf_headers(client), json=payload)

    assert response.status_code == 422


def test_login_sets_cookie_and_session_stores_only_a_hash(client: TestClient) -> None:
    assert register(client).status_code == 201

    response = login(client)

    assert response.status_code == 200
    raw_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert raw_token is not None
    with client.auth_session_factory() as db:
        session = db.query(UserSession).one()
        assert session.token_hash != raw_token
        assert raw_token not in session.token_hash


def test_login_failure_does_not_reveal_whether_email_exists(client: TestClient) -> None:
    assert register(client).status_code == 201

    unknown_email = login(client, email="unknown@example.com")
    wrong_password = login(client, password="wrong-long-password")

    assert unknown_email.status_code == wrong_password.status_code == 401
    assert unknown_email.json() == wrong_password.json() == {"detail": "Invalid email or password"}


def test_inactive_user_cannot_login(client: TestClient) -> None:
    assert register(client).status_code == 201
    with client.auth_session_factory() as db:
        user = db.query(User).one()
        user.is_active = False
        db.commit()

    response = login(client)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid email or password"}


def test_session_cookie_has_http_only_samesite_and_secure_attributes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert register(client).status_code == 201
    csrf_request_headers = csrf_headers(client)
    monkeypatch.setattr(settings, "session_cookie_secure", True)

    response = client.post(
        "/auth/login",
        headers=csrf_request_headers,
        json={"email": "user@example.com", "password": "long-safe-password"},
    )

    session_cookie = next(
        cookie
        for cookie in response.headers.get_list("set-cookie")
        if cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    )
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Secure" in session_cookie
    assert "Path=/" in session_cookie
    assert "Max-Age=86400" in session_cookie


def test_authenticated_me_and_unauthenticated_me(client: TestClient) -> None:
    assert client.get("/auth/me").status_code == 401
    assert register(client).status_code == 201
    assert login(client).status_code == 200

    response = client.get("/auth/me")

    assert response.status_code == 200
    assert response.json() == {"id": 1, "email": "user@example.com"}


@pytest.mark.parametrize("state", ["expired", "revoked"])
def test_expired_or_revoked_session_is_rejected(client: TestClient, state: str) -> None:
    assert register(client).status_code == 201
    assert login(client).status_code == 200
    with client.auth_session_factory() as db:
        session = db.query(UserSession).one()
        if state == "expired":
            session.expires_at = current_taipei_time() - timedelta(seconds=1)
        else:
            session.revoked_at = current_taipei_time()
        db.commit()

    assert client.get("/auth/me").status_code == 401


def test_logout_revokes_the_current_session_and_clears_cookies(client: TestClient) -> None:
    assert register(client).status_code == 201
    assert login(client).status_code == 200

    response = client.post("/auth/logout", headers=csrf_headers(client))

    assert response.status_code == 204
    assert client.get("/auth/me").status_code == 401
    with client.auth_session_factory() as db:
        assert db.query(UserSession).one().revoked_at is not None


def test_csrf_and_origin_protection(client: TestClient) -> None:
    missing_origin = client.post(
        "/auth/register",
        json={"email": "user@example.com", "password": "long-safe-password"},
    )
    missing_csrf = client.post(
        "/auth/register",
        headers={"Origin": ALLOWED_ORIGIN},
        json={"email": "user@example.com", "password": "long-safe-password"},
    )
    wrong_origin = client.post(
        "/auth/register",
        headers={"Origin": "https://attacker.example", "X-CSRF-Token": "wrong"},
        json={"email": "user@example.com", "password": "long-safe-password"},
    )
    valid = register(client)

    assert missing_origin.status_code == 403
    assert missing_origin.json() == {"detail": "Request origin is not allowed"}
    assert missing_csrf.status_code == 403
    assert missing_csrf.json() == {
        "detail": {"code": CSRF_ERROR_CODE, "message": "CSRF validation failed"}
    }
    assert wrong_origin.status_code == 403
    assert wrong_origin.json() == {"detail": "Request origin is not allowed"}
    assert valid.status_code == 201


def test_csrf_token_issuance_allows_missing_origin_but_rejects_disallowed_origin(client: TestClient) -> None:
    # Browsers do not guarantee Origin on same-origin GET requests. This only
    # issues an unreadable random token; mutations still require strict Origin
    # validation and a matching CSRF cookie/header pair.
    missing_origin = client.get("/auth/csrf")
    allowed_origin = client.get("/auth/csrf", headers={"Origin": ALLOWED_ORIGIN})
    disallowed_origin = client.get("/auth/csrf", headers={"Origin": "https://attacker.example"})

    assert missing_origin.status_code == 200
    assert missing_origin.json()["csrf_token"]
    assert allowed_origin.status_code == 200
    assert disallowed_origin.status_code == 403
    assert disallowed_origin.json() == {"detail": "Request origin is not allowed"}


def test_tasks_endpoint_requires_authentication_after_ownership_cutover(client: TestClient) -> None:
    response = client.get("/tasks")

    assert response.status_code == 401
