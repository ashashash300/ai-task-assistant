from datetime import datetime, timedelta
import os
from pathlib import Path
import subprocess
import sys
from zoneinfo import ZoneInfo

import pytest
from google.genai import types
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_task_assistant.db.base import Base
from ai_task_assistant.core.datetime import TAIPEI_TIMEZONE
from ai_task_assistant.models.conversation import Conversation
from ai_task_assistant.models.task import Task
from ai_task_assistant.models.user import User
from ai_task_assistant.services.conversation_codec import (
    ConversationCodecError,
    GEMINI_HISTORY_SCHEMA_VERSION,
    decode_gemini_history,
    encode_gemini_history,
)
from ai_task_assistant.services import conversation_service


USER_ID = 1


def make_text_history() -> list[types.Content]:
    return [
        types.Content(role="user", parts=[types.Part.from_text(text="hello")]),
        types.Content(role="model", parts=[types.Part.from_text(text="hi")]),
    ]


def make_tool_history() -> list[types.Content]:
    return [
        types.Content(role="user", parts=[types.Part.from_text(text="list tasks")]),
        types.Content(
            role="model",
            parts=[types.Part.from_function_call(name="list_tasks", args={})],
        ),
        types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        name="list_tasks",
                        response={"ok": True, "tasks": []},
                    )
                )
            ],
        ),
        types.Content(role="model", parts=[types.Part.from_text(text="No tasks.")]),
    ]


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as db:
            db.add(User(id=USER_ID, email="conversation@example.com", password_hash="test-hash"))
            db.commit()
            yield db
    finally:
        Base.metadata.drop_all(engine)


def test_conversations_table_is_created_without_removing_tasks_table() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        assert {"conversations", "tasks"}.issubset(inspect(engine).get_table_names())
    finally:
        Base.metadata.drop_all(engine)


def test_conversation_fields_and_openai_response_id_round_trip(session: Session) -> None:
    conversation = Conversation(
        user_id=USER_ID,
        conversation_id="openai-demo",
        provider="openai",
        openai_response_id="resp_final_123",
        turn_count=2,
    )
    session.add(conversation)
    session.commit()
    conversation_id = conversation.id
    session.expire_all()

    restored = session.get(Conversation, conversation_id)

    assert restored is not None
    assert restored.conversation_id == "openai-demo"
    assert restored.provider == "openai"
    assert restored.openai_response_id == "resp_final_123"
    assert restored.gemini_history_json is None
    assert restored.turn_count == 2
    assert restored.state_version == 1


def test_gemini_text_history_round_trips_through_json() -> None:
    encoded = encode_gemini_history(make_text_history())
    restored = decode_gemini_history(encoded)

    assert encoded["schema_version"] == GEMINI_HISTORY_SCHEMA_VERSION
    assert [content.model_dump(mode="json", exclude_none=True) for content in restored] == encoded[
        "contents"
    ]


def test_gemini_tool_history_round_trips_through_json(session: Session) -> None:
    encoded = encode_gemini_history(make_tool_history())
    conversation = Conversation(
        user_id=USER_ID,
        conversation_id="gemini-demo",
        provider="gemini",
        gemini_history_json=encoded,
    )
    session.add(conversation)
    session.commit()
    session.expire_all()

    restored = session.get(Conversation, conversation.id)

    assert restored is not None
    assert restored.gemini_history_json == encoded
    history = decode_gemini_history(restored.gemini_history_json)
    assert history[1].parts[0].function_call.name == "list_tasks"
    assert history[2].parts[0].function_response.name == "list_tasks"


def test_unsupported_gemini_history_schema_version_fails() -> None:
    with pytest.raises(ConversationCodecError, match="Unsupported Gemini history schema version"):
        decode_gemini_history({"schema_version": 2, "contents": []})


def test_invalid_gemini_content_fails_without_echoing_payload() -> None:
    with pytest.raises(ConversationCodecError, match="Gemini conversation history is invalid"):
        decode_gemini_history(
            {"schema_version": 1, "contents": [{"role": "user", "parts": "invalid"}]}
        )


def test_unpaired_gemini_function_call_fails() -> None:
    with pytest.raises(ConversationCodecError, match="Gemini conversation history is invalid"):
        encode_gemini_history(
            [
                types.Content(role="user", parts=[types.Part.from_text(text="list tasks")]),
                types.Content(
                    role="model",
                    parts=[types.Part.from_function_call(name="list_tasks", args={})],
                ),
            ]
        )


def test_conversation_timestamps_restore_asia_taipei_awareness(session: Session) -> None:
    conversation = Conversation(user_id=USER_ID, conversation_id="time-demo", provider="openai")
    session.add(conversation)
    session.commit()
    session.expire_all()

    restored = session.get(Conversation, conversation.id)

    assert restored is not None
    assert restored.created_at.tzinfo == ZoneInfo("Asia/Taipei")
    assert restored.updated_at.tzinfo == ZoneInfo("Asia/Taipei")
    assert restored.created_at.utcoffset() is not None
    assert restored.updated_at.utcoffset() is not None


def test_state_version_defaults_to_one(session: Session) -> None:
    conversation = Conversation(user_id=USER_ID, conversation_id="version-demo", provider="openai")
    session.add(conversation)
    session.commit()

    assert conversation.state_version == 1


@pytest.mark.parametrize(
    ("conversation_id", "provider", "turn_count", "state_version"),
    [
        ("invalid-provider", "other", 0, 1),
        ("negative-turns", "openai", -1, 1),
        ("invalid-version", "openai", 0, 0),
    ],
)
def test_conversation_database_constraints_are_enforced(
    session: Session,
    conversation_id: str,
    provider: str,
    turn_count: int,
    state_version: int,
) -> None:
    session.add(
        Conversation(
            user_id=USER_ID,
            conversation_id=conversation_id,
            provider=provider,
            turn_count=turn_count,
            state_version=state_version,
        )
    )

    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_existing_task_table_remains_usable_with_conversations(session: Session) -> None:
    task = Task(user_id=USER_ID, title="Existing task")
    conversation = Conversation(user_id=USER_ID, conversation_id="task-compatible", provider="openai")
    session.add_all([task, conversation])
    session.commit()

    assert session.get(Task, task.id) is not None
    assert session.get(Conversation, conversation.id) is not None


def add_openai_conversation(
    session: Session, conversation_id: str, updated_at: datetime, turn_count: int = 1
) -> Conversation:
    conversation = Conversation(
        user_id=USER_ID,
        conversation_id=conversation_id,
        provider="openai",
        openai_response_id=f"resp-{conversation_id}",
        turn_count=turn_count,
        created_at=updated_at,
        updated_at=updated_at,
    )
    session.add(conversation)
    session.commit()
    return conversation


def test_cleanup_keeps_conversations_updated_within_retention(session: Session) -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=TAIPEI_TIMEZONE)
    add_openai_conversation(session, "recent", now - timedelta(days=29))

    deleted = conversation_service.cleanup_expired_conversations(session, user_id=USER_ID, now=now)

    assert deleted == 0
    assert conversation_service.get_conversation(session, user_id=USER_ID, conversation_id="recent") is not None


def test_cleanup_deletes_only_conversations_strictly_older_than_cutoff(session: Session) -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=TAIPEI_TIMEZONE)
    add_openai_conversation(session, "at-cutoff", now - timedelta(days=30))
    add_openai_conversation(session, "expired", now - timedelta(days=30, seconds=1))

    deleted = conversation_service.cleanup_expired_conversations(session, user_id=USER_ID, now=now)

    assert deleted == 1
    assert conversation_service.get_conversation(session, user_id=USER_ID, conversation_id="at-cutoff") is not None
    assert conversation_service.get_conversation(session, user_id=USER_ID, conversation_id="expired") is None


def test_cleanup_does_not_delete_tasks(session: Session) -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=TAIPEI_TIMEZONE)
    task = Task(user_id=USER_ID, title="Must remain")
    session.add(task)
    session.commit()
    add_openai_conversation(session, "expired-with-task", now - timedelta(days=31))

    conversation_service.cleanup_expired_conversations(session, user_id=USER_ID, now=now)

    assert session.get(Task, task.id) is not None


def test_expired_conversation_id_can_start_a_new_conversation(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=TAIPEI_TIMEZONE)
    add_openai_conversation(session, "reused-id", now - timedelta(days=31))
    monkeypatch.setattr(conversation_service, "current_taipei_time", lambda: now)

    state = conversation_service.prepare_conversation(session, user_id=USER_ID, conversation_id="reused-id", provider="openai")
    new_state = conversation_service.save_response(
        session,
        user_id=USER_ID,
        conversation_id="reused-id",
        provider_state="resp-new",
        provider="openai",
        expected_state_version=None,
        expected_row_id=None,
    )

    assert state is None
    assert new_state.last_response_id == "resp-new"
    assert new_state.turn_count == 1


def test_cleanup_runs_before_new_conversation_limit_check(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=TAIPEI_TIMEZONE)
    for index in range(conversation_service.MAX_CONVERSATIONS):
        add_openai_conversation(
            session,
            f"expired-limit-{index}",
            now - timedelta(days=31),
        )
    monkeypatch.setattr(conversation_service, "current_taipei_time", lambda: now)

    state = conversation_service.prepare_conversation(session, user_id=USER_ID, conversation_id="new-after-cleanup", provider="openai")

    assert state is None
    assert session.query(Conversation).count() == 0


def test_cleanup_failure_rolls_back(session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    rollback_called = False

    def fail_execute(*_args, **_kwargs):
        raise SQLAlchemyError("database unavailable")

    original_rollback = session.rollback

    def record_rollback() -> None:
        nonlocal rollback_called
        rollback_called = True
        original_rollback()

    monkeypatch.setattr(session, "execute", fail_execute)
    monkeypatch.setattr(session, "rollback", record_rollback)

    with pytest.raises(conversation_service.ConversationStorageError):
        conversation_service.cleanup_expired_conversations(session, user_id=USER_ID)

    assert rollback_called is True


def test_cleanup_cannot_revive_an_inflight_expired_conversation(conversation_engine) -> None:
    now = datetime(2026, 10, 21, 12, 0, tzinfo=TAIPEI_TIMEZONE)
    with Session(conversation_engine) as setup_session:
        add_openai_conversation(setup_session, "inflight-expired", now - timedelta(days=31))

    with Session(conversation_engine) as request_a_session:
        stale_state = conversation_service.get_conversation(
            request_a_session, user_id=USER_ID, conversation_id="inflight-expired"
        )

    with Session(conversation_engine) as request_b_session:
        assert conversation_service.cleanup_expired_conversations(
            request_b_session, user_id=USER_ID, now=now
        ) == 1

    assert stale_state is not None
    with Session(conversation_engine) as request_a_save_session:
        with pytest.raises(conversation_service.ConversationConflictError):
            conversation_service.save_response(
                request_a_save_session,
                user_id=USER_ID,
                conversation_id="inflight-expired",
                provider_state="resp-after-cleanup",
                provider="openai",
                expected_state_version=stale_state.state_version,
                expected_row_id=stale_state.row_id,
            )

    with Session(conversation_engine) as verification_session:
        assert conversation_service.get_conversation(
            verification_session, user_id=USER_ID, conversation_id="inflight-expired"
        ) is None


@pytest.fixture
def conversation_engine():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(User(id=USER_ID, email="engine@example.com", password_hash="test-hash"))
        db.commit()
    try:
        yield engine
    finally:
        Base.metadata.drop_all(engine)


def test_openai_conversation_state_round_trips_across_sessions(conversation_engine) -> None:
    with Session(conversation_engine) as first_session:
        conversation_service.save_response(
            first_session,
            user_id=USER_ID,
            conversation_id="openai-cross-session",
            provider_state="resp_first",
            provider="openai",
            expected_state_version=None,
            expected_row_id=None,
        )

    with Session(conversation_engine) as second_session:
        restored = conversation_service.prepare_conversation(
            second_session, user_id=USER_ID, conversation_id="openai-cross-session", provider="openai"
        )

    assert restored is not None
    assert restored.provider_state == "resp_first"
    assert restored.last_response_id == "resp_first"
    assert restored.turn_count == 1
    assert restored.state_version == 1


def test_gemini_history_round_trips_across_sessions(conversation_engine) -> None:
    history = make_tool_history()
    with Session(conversation_engine) as first_session:
        conversation_service.save_response(
            first_session,
            user_id=USER_ID,
            conversation_id="gemini-cross-session",
            provider_state=history,
            provider="gemini",
            expected_state_version=None,
            expected_row_id=None,
        )

    with Session(conversation_engine) as second_session:
        restored = conversation_service.prepare_conversation(
            second_session, user_id=USER_ID, conversation_id="gemini-cross-session", provider="gemini"
        )

    assert restored is not None
    assert restored.last_response_id is None
    assert restored.provider == "gemini"
    assert [content.model_dump(mode="json", exclude_none=True) for content in restored.provider_state] == (
        encode_gemini_history(history)["contents"]
    )


def test_stale_conversation_update_raises_conflict(conversation_engine) -> None:
    with Session(conversation_engine) as first_session:
        conversation_service.save_response(
            first_session,
            user_id=USER_ID,
            conversation_id="stale-update",
            provider_state="resp_first",
            provider="openai",
            expected_state_version=None,
            expected_row_id=None,
        )

    with Session(conversation_engine) as stale_session:
        stale_state = conversation_service.get_conversation(
            stale_session, user_id=USER_ID, conversation_id="stale-update"
        )

    with Session(conversation_engine) as current_session:
        current_state = conversation_service.get_conversation(
            current_session, user_id=USER_ID, conversation_id="stale-update"
        )
        assert current_state is not None
        conversation_service.save_response(
            current_session,
            user_id=USER_ID,
            conversation_id="stale-update",
            provider_state="resp_current",
            provider="openai",
            expected_state_version=current_state.state_version,
            expected_row_id=current_state.row_id,
        )

    assert stale_state is not None
    with Session(conversation_engine) as stale_save_session:
        with pytest.raises(conversation_service.ConversationConflictError):
            conversation_service.save_response(
                stale_save_session,
                user_id=USER_ID,
                conversation_id="stale-update",
                provider_state="resp_stale",
                provider="openai",
                expected_state_version=stale_state.state_version,
                expected_row_id=stale_state.row_id,
            )


def test_concurrent_create_unique_conflict_raises_conflict(conversation_engine) -> None:
    with Session(conversation_engine) as first_session:
        conversation_service.save_response(
            first_session,
            user_id=USER_ID,
            conversation_id="duplicate-create",
            provider_state="resp_first",
            provider="openai",
            expected_state_version=None,
            expected_row_id=None,
        )

    with Session(conversation_engine) as second_session:
        with pytest.raises(conversation_service.ConversationConflictError):
            conversation_service.save_response(
                second_session,
                user_id=USER_ID,
                conversation_id="duplicate-create",
                provider_state="resp_duplicate",
                provider="openai",
                expected_state_version=None,
                expected_row_id=None,
            )


def test_pending_deletion_remains_process_local_after_state_persists(conversation_engine) -> None:
    conversation_service.clear_pending_deletions()
    with Session(conversation_engine) as session:
        conversation_service.save_response(
            session,
            user_id=USER_ID,
            conversation_id="pending-is-not-persisted",
            provider_state="resp_first",
            provider="openai",
            expected_state_version=None,
            expected_row_id=None,
        )
    conversation_service.save_pending_deletion(
        user_id=USER_ID,
        conversation_id="pending-is-not-persisted",
        request=conversation_service.PendingDeletionRequest(task_id=1, title="Delete me"),
    )

    conversation_service.clear_pending_deletions()

    with Session(conversation_engine) as restarted_session:
        assert conversation_service.get_conversation(
            restarted_session, user_id=USER_ID, conversation_id="pending-is-not-persisted"
        ) is not None
    assert conversation_service.get_pending_deletion(
        user_id=USER_ID, conversation_id="pending-is-not-persisted"
    ) is None


def _run_persistence_subprocess(database_path: Path, script: str) -> None:
    """Run against only a temporary SQLite file, without loading the project .env."""

    project_root = Path(__file__).resolve().parents[1]
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(project_root / "src"),
        "DATABASE_URL": f"sqlite:///{database_path}",
        "AI_PROVIDER": "openai",
        "OPENAI_MODEL": "test-model",
        "GEMINI_MODEL": "test-model",
    }
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=database_path.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, "Persistence subprocess failed"


def test_conversation_state_survives_a_real_process_restart(tmp_path: Path) -> None:
    """A second Python process restores SQLite state but not pending deletion authorization."""

    database_path = tmp_path / "conversation-restart.sqlite"

    _run_persistence_subprocess(
        database_path,
        """
from google.genai import types

from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import SessionLocal, engine
from ai_task_assistant.models.user import User
from ai_task_assistant.services.conversation_service import (
    PendingDeletionRequest,
    save_pending_deletion,
    save_response,
)

Base.metadata.create_all(bind=engine)
history = [
    types.Content(role="user", parts=[types.Part.from_text(text="hello")]),
    types.Content(role="model", parts=[types.Part.from_text(text="hi")]),
]
with SessionLocal() as db:
    db.add(User(id=1, email="restart@example.com", password_hash="test-hash"))
    db.commit()
    save_response(
        db, user_id=1, conversation_id="restart-openai", provider_state="resp_final_restart",
        provider="openai", expected_state_version=None, expected_row_id=None,
    )
    save_response(
        db, user_id=1, conversation_id="restart-gemini", provider_state=history,
        provider="gemini", expected_state_version=None, expected_row_id=None,
    )
save_pending_deletion(
    user_id=1, conversation_id="restart-openai",
    request=PendingDeletionRequest(task_id=1, title="Not persisted")
)
""",
    )

    _run_persistence_subprocess(
        database_path,
        """
from ai_task_assistant.db.session import SessionLocal
from ai_task_assistant.services.conversation_service import (
    get_conversation,
    get_pending_deletion,
)

with SessionLocal() as db:
    openai_state = get_conversation(db, user_id=1, conversation_id="restart-openai")
    gemini_state = get_conversation(db, user_id=1, conversation_id="restart-gemini")

assert openai_state is not None
assert openai_state.provider_state == "resp_final_restart"
assert openai_state.turn_count == 1
assert gemini_state is not None
assert gemini_state.provider == "gemini"
assert gemini_state.provider_state[0].role == "user"
assert gemini_state.provider_state[1].role == "model"
assert get_pending_deletion(user_id=1, conversation_id="restart-openai") is None
""",
    )
