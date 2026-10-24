from datetime import timedelta

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_task_assistant.core.datetime import TAIPEI_TIMEZONE, current_taipei_time
from ai_task_assistant.core.security import hash_password, hash_session_token, normalize_email
from ai_task_assistant.db.base import Base
import ai_task_assistant.models  # Register all metadata without opening an application DB.
from ai_task_assistant.models.session import UserSession
from ai_task_assistant.models.user import User


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            yield db
    finally:
        Base.metadata.drop_all(engine)


def create_user(session: Session, email: str = "user@example.com") -> User:
    user = User(email=normalize_email(email), password_hash=hash_password("safe password"))
    session.add(user)
    session.commit()
    return user


def test_user_email_unique_constraint(session: Session) -> None:
    create_user(session, "user@example.com")
    session.add(User(email=" USER@EXAMPLE.COM ", password_hash=hash_password("another password")))

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_session_token_hash_unique_constraint_and_foreign_key(session: Session) -> None:
    user = create_user(session)
    token_hash = hash_session_token("test-session-token")
    expires_at = current_taipei_time() + timedelta(hours=1)
    session.add(UserSession(user_id=user.id, token_hash=token_hash, expires_at=expires_at))
    session.commit()
    session.add(UserSession(user_id=user.id, token_hash=token_hash, expires_at=expires_at))

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(
        UserSession(
            user_id=user.id + 10_000,
            token_hash=hash_session_token("different-test-session-token"),
            expires_at=expires_at,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_session_stores_only_token_hash_and_restores_taipei_datetimes(session: Session) -> None:
    user = create_user(session)
    raw_token = "not-a-real-token-used-only-for-this-isolated-test"
    expires_at = current_taipei_time() + timedelta(hours=1)
    session_row = UserSession(
        user_id=user.id,
        token_hash=hash_session_token(raw_token),
        expires_at=expires_at,
    )
    session.add(session_row)
    session.commit()
    session.expire_all()

    restored_user = session.get(User, user.id)
    restored_session = session.get(UserSession, session_row.id)

    assert restored_user is not None
    assert restored_session is not None
    assert restored_session.token_hash != raw_token
    assert raw_token not in restored_session.token_hash
    assert restored_user.created_at.tzinfo == TAIPEI_TIMEZONE
    assert restored_user.updated_at.tzinfo == TAIPEI_TIMEZONE
    assert restored_session.created_at.tzinfo == TAIPEI_TIMEZONE
    assert restored_session.last_seen_at.tzinfo == TAIPEI_TIMEZONE
    assert restored_session.expires_at.tzinfo == TAIPEI_TIMEZONE


def test_auth_metadata_includes_owner_scoped_task_and_conversation_columns() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        inspector = inspect(engine)
        assert {"users", "sessions", "tasks", "conversations"}.issubset(
            inspector.get_table_names()
        )
        assert {column["name"] for column in inspector.get_columns("tasks")} == {
            "id",
            "user_id",
            "title",
            "description",
            "due_date",
            "priority",
            "status",
            "created_at",
            "updated_at",
        }
        assert {column["name"] for column in inspector.get_columns("conversations")} == {
            "id",
            "user_id",
            "conversation_id",
            "provider",
            "openai_response_id",
            "gemini_history_json",
            "turn_count",
            "state_version",
            "created_at",
            "updated_at",
        }
        assert inspector.get_foreign_keys("sessions") == [
            {
                "name": None,
                "constrained_columns": ["user_id"],
                "referred_schema": None,
                "referred_table": "users",
                "referred_columns": ["id"],
                "options": {},
            }
        ]
    finally:
        Base.metadata.drop_all(engine)
