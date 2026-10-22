from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ai_task_assistant.api.dependencies.auth import require_csrf_protection, require_current_user
from ai_task_assistant.core.config import settings
from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import get_db
import ai_task_assistant.api.routes.chat as chat_route
import ai_task_assistant.main as main_module
from ai_task_assistant.main import app
from ai_task_assistant.models.user import User
from ai_task_assistant.models.chat_quota import ChatDailyQuota
from ai_task_assistant.services.chat_quota_service import (
    ChatQuotaExceeded,
    ChatQuotaStorageError,
    cleanup_expired_chat_quotas,
    reserve_daily_chat_quota,
)
from ai_task_assistant.services import rate_limit_service
from ai_task_assistant.services.llm_provider import ProviderError, ProviderResult


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        user = User(email="quota@example.com", password_hash="test-hash")
        db.add(user)
        db.commit()
        db.refresh(user)

    def override_get_db() -> Generator[Session, None, None]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_current_user] = lambda: user
    app.dependency_overrides[require_csrf_protection] = lambda: None
    original_engine = main_module.engine
    main_module.engine = engine
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        main_module.engine = original_engine
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


class FakeProvider:
    def __init__(self, *, failure: Exception | None = None, entered: Event | None = None, release: Event | None = None) -> None:
        self.calls = 0
        self.failure = failure
        self.entered = entered
        self.release = release

    def run_turn(self, **_kwargs) -> ProviderResult:
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=3)
        if self.failure is not None:
            raise self.failure
        return ProviderResult(response="ok", conversation_state="response-id")


def install_fake_provider(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr("ai_task_assistant.services.ai_service.get_provider", lambda: provider)


def test_chat_minute_limit_and_daily_quota_are_checked_before_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    install_fake_provider(monkeypatch, provider)
    monkeypatch.setattr(settings, "chat_user_limit", 1)
    first = client.post("/chat", json={"message": "one"})
    second = client.post("/chat", json={"message": "two"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "60"
    assert provider.calls == 1

    rate_limit_service.clear_rate_limits()
    monkeypatch.setattr(settings, "chat_user_limit", 10)
    # The successful minute-limit request already reserved one durable quota.
    monkeypatch.setattr(settings, "chat_daily_quota", 2)
    assert client.post("/chat", json={"message": "daily one"}).status_code == 200
    daily = client.post("/chat", json={"message": "daily two"})
    assert daily.status_code == 429
    assert daily.json() == {"detail": "Chat quota exceeded"}


def test_durable_daily_quota_isolated_per_user_and_resets_at_taipei_midnight() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    with factory() as db:
        db.add_all(
            [
                User(email="quota-a@example.com", password_hash="hash"),
                User(email="quota-b@example.com", password_hash="hash"),
            ]
        )
        db.commit()
        users = list(db.scalars(select(User).order_by(User.id)))
        before_midnight = datetime(2026, 10, 22, 23, 59, tzinfo=ZoneInfo("Asia/Taipei"))
        after_midnight = datetime(2026, 10, 23, 0, 0, tzinfo=ZoneInfo("Asia/Taipei"))
        reserve_daily_chat_quota(db, user_id=users[0].id, daily_limit=1, now=before_midnight)
        with pytest.raises(ChatQuotaExceeded):
            reserve_daily_chat_quota(db, user_id=users[0].id, daily_limit=1, now=before_midnight)
        reserve_daily_chat_quota(db, user_id=users[1].id, daily_limit=1, now=before_midnight)
        reserve_daily_chat_quota(db, user_id=users[0].id, daily_limit=1, now=after_midnight)
    Base.metadata.drop_all(bind=engine)


def test_durable_daily_quota_survives_engine_restart(tmp_path) -> None:
    database_path = tmp_path / "quota-restart.db"
    engine_a = create_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=engine_a)
    factory_a = sessionmaker(bind=engine_a)
    with factory_a() as db:
        user = User(email="restart-quota@example.com", password_hash="hash")
        db.add(user)
        db.commit()
        reserve_daily_chat_quota(
            db,
            user_id=user.id,
            daily_limit=1,
            now=datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Taipei")),
        )
    engine_a.dispose()

    engine_b = create_engine(f"sqlite:///{database_path}")
    factory_b = sessionmaker(bind=engine_b)
    with factory_b() as db:
        user = db.scalar(select(User).where(User.email == "restart-quota@example.com"))
        assert user is not None
        with pytest.raises(ChatQuotaExceeded):
            reserve_daily_chat_quota(
                db,
                user_id=user.id,
                daily_limit=1,
                now=datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Taipei")),
            )
    engine_b.dispose()


def test_concurrent_durable_quota_reservations_are_atomic(tmp_path) -> None:
    database_path = tmp_path / "quota-concurrency.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False, "timeout": 3},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        user = User(email="atomic-quota@example.com", password_hash="hash")
        db.add(user)
        db.commit()
        user_id = user.id

    gate = Event()

    def reserve() -> str:
        with factory() as db:
            gate.wait(timeout=3)
            try:
                reserve_daily_chat_quota(db, user_id=user_id, daily_limit=1)
                return "reserved"
            except ChatQuotaExceeded:
                return "exceeded"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(reserve), executor.submit(reserve)]
        gate.set()
        results = sorted(future.result(timeout=5) for future in futures)
    assert results == ["exceeded", "reserved"]
    with factory() as db:
        row = db.scalar(select(ChatDailyQuota).where(ChatDailyQuota.user_id == user_id))
        assert row is not None
        assert row.request_count == 1
    engine.dispose()


def test_expired_quota_cleanup_never_deletes_current_day() -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)
    today = datetime(2026, 10, 22, 12, tzinfo=ZoneInfo("Asia/Taipei"))
    with factory() as db:
        user = User(email="cleanup-quota@example.com", password_hash="hash")
        db.add(user)
        db.commit()
        reserve_daily_chat_quota(db, user_id=user.id, daily_limit=5, now=today)
        reserve_daily_chat_quota(
            db,
            user_id=user.id,
            daily_limit=5,
            now=datetime(2026, 10, 20, 12, tzinfo=ZoneInfo("Asia/Taipei")),
        )
        assert cleanup_expired_chat_quotas(db, before_date=today.date()) == 1
        assert db.scalar(select(ChatDailyQuota).where(ChatDailyQuota.quota_date == today.date()))
    Base.metadata.drop_all(bind=engine)


def test_provider_exception_releases_chat_slot(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    failing = FakeProvider(failure=ProviderError("AI service is temporarily unavailable"))
    install_fake_provider(monkeypatch, failing)
    monkeypatch.setattr(settings, "chat_concurrent_limit", 1)
    assert client.post("/chat", json={"message": "fails"}).status_code == 503
    succeeding = FakeProvider()
    install_fake_provider(monkeypatch, succeeding)
    assert client.post("/chat", json={"message": "works"}).status_code == 200


def test_provider_failure_consumes_durable_daily_quota(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "chat_daily_quota", 1)
    install_fake_provider(monkeypatch, FakeProvider(failure=ProviderError("safe failure")))
    assert client.post("/chat", json={"message": "fails"}).status_code == 503
    succeeding = FakeProvider()
    install_fake_provider(monkeypatch, succeeding)
    response = client.post("/chat", json={"message": "must not call provider"})
    assert response.status_code == 429
    assert response.headers["Retry-After"]
    assert succeeding.calls == 0


def test_quota_storage_failure_fails_closed_before_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    install_fake_provider(monkeypatch, provider)

    def fail_storage(*_args, **_kwargs) -> int:
        raise ChatQuotaStorageError("internal")

    monkeypatch.setattr(chat_route, "reserve_daily_chat_quota", fail_storage)
    response = client.post("/chat", json={"message": "blocked by quota storage"})
    assert response.status_code == 503
    assert response.json() == {"detail": "Chat quota is temporarily unavailable"}
    assert provider.calls == 0


def test_sqlite_quota_operational_error_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A SQLite busy/operational failure must not admit an AI provider call."""

    class FailingSession:
        def __init__(self) -> None:
            self.rolled_back = False

        def scalar(self, _statement):
            raise OperationalError("INSERT", {}, Exception("database is locked"))

        def rollback(self) -> None:
            self.rolled_back = True

    db = FailingSession()
    with pytest.raises(ChatQuotaStorageError):
        reserve_daily_chat_quota(db, user_id=1, daily_limit=1)
    assert db.rolled_back is True


def test_concurrent_chat_requests_allow_only_one_in_flight(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = Event(), Event()
    provider = FakeProvider(entered=entered, release=release)
    install_fake_provider(monkeypatch, provider)
    monkeypatch.setattr(settings, "chat_concurrent_limit", 1)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.post, "/chat", json={"message": "slow"})
        assert entered.wait(timeout=3)
        second = client.post("/chat", json={"message": "blocked"})
        release.set()
        assert first.result(timeout=3).status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"] == "1"
    assert provider.calls == 1
