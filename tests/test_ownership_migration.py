from datetime import timedelta
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.db.base import Base
import ai_task_assistant.models  # Register source-schema models without starting FastAPI.
from ai_task_assistant.models.user import User
from scripts import migrate_user_ownership as migration


def create_legacy_database(tmp_path: Path) -> Path:
    database_path = tmp_path / "legacy-source.sqlite"
    engine = create_engine(f"sqlite:///{database_path}")
    # Build the pre-ownership schema explicitly. The current ORM is already
    # owner-scoped and must never be used to manufacture legacy fixtures.
    User.__table__.create(engine)
    now = current_taipei_time()
    try:
        with sqlite3.connect(database_path) as connection:
            connection.execute(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY, title VARCHAR(255) NOT NULL,
                    description TEXT, due_date DATETIME, priority VARCHAR(6) NOT NULL,
                    status VARCHAR(9) NOT NULL, created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE conversations (
                    id INTEGER PRIMARY KEY, conversation_id VARCHAR(64) NOT NULL UNIQUE,
                    provider VARCHAR(16) NOT NULL, openai_response_id VARCHAR(255),
                    gemini_history_json JSON, turn_count INTEGER NOT NULL,
                    state_version INTEGER NOT NULL, created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    CONSTRAINT ck_conversations_provider CHECK (provider IN ('openai', 'gemini')),
                    CONSTRAINT ck_conversations_turn_count CHECK (turn_count >= 0),
                    CONSTRAINT ck_conversations_state_version CHECK (state_version > 0)
                )
                """
            )
            for title in ("Legacy task one", "Legacy task two"):
                connection.execute(
                    """INSERT INTO tasks(title, priority, status, created_at, updated_at)
                    VALUES (?, 'medium', 'pending', ?, ?)""",
                    (title, now.isoformat(), now.isoformat()),
                )
            connection.execute(
                """INSERT INTO conversations(
                    conversation_id, provider, gemini_history_json, turn_count,
                    state_version, created_at, updated_at
                ) VALUES (?, 'gemini', ?, 1, 1, ?, ?)""",
                (
                    "legacy-conversation",
                    json.dumps({
                        "schema_version": 1,
                        "contents": [{"role": "user", "parts": [{"text": "private history"}]}],
                    }),
                    (now - timedelta(days=1)).isoformat(),
                    now.isoformat(),
                ),
            )
    finally:
        engine.dispose()
    return database_path


def table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        return {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }


def test_dry_run_does_not_modify_legacy_source(tmp_path: Path) -> None:
    database_path = create_legacy_database(tmp_path)
    before = hashlib.sha256(database_path.read_bytes()).hexdigest()

    report = migration.dry_run(database_path)

    assert report.mode == "dry-run"
    assert report.legacy_task_count == 2
    assert report.legacy_conversation_count == 1
    assert report.backup_path is None
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before
    assert "legacy_tasks" not in table_names(database_path)
    assert "legacy_conversations" not in table_names(database_path)
    assert "schema_migrations" not in table_names(database_path)
    with sqlite3.connect(database_path) as connection:
        assert "user_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(tasks)")
        }
        assert "user_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(conversations)")
        }


def test_apply_preserves_legacy_rows_and_creates_empty_owner_scoped_tables(
    tmp_path: Path,
) -> None:
    database_path = create_legacy_database(tmp_path)

    report = migration.apply(database_path)

    assert report.mode == "apply"
    assert report.legacy_task_count == 2
    assert report.legacy_conversation_count == 1
    assert report.backup_path is not None
    assert Path(report.backup_path).is_file()

    with sqlite3.connect(database_path) as connection:
        tables = table_names(database_path)
        assert {"legacy_tasks", "legacy_conversations", "tasks", "conversations"}.issubset(tables)
        assert connection.execute("SELECT COUNT(*) FROM legacy_tasks").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM legacy_conversations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
        assert json.loads(
            connection.execute("SELECT gemini_history_json FROM legacy_conversations").fetchone()[0]
        ) == {
            "schema_version": 1,
            "contents": [{"role": "user", "parts": [{"text": "private history"}]}],
        }
        task_columns = {row[1]: row for row in connection.execute("PRAGMA table_info(tasks)")}
        conversation_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(conversations)")
        }
        assert task_columns["user_id"][3] == 1
        assert conversation_columns["user_id"][3] == 1
        assert connection.execute(
            "SELECT version FROM schema_migrations"
        ).fetchone()[0] == migration.MIGRATION_VERSION


def test_target_conversation_unique_scope_and_foreign_keys(tmp_path: Path) -> None:
    database_path = create_legacy_database(tmp_path)
    migration.apply(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for user_id, email in [(1, "first@example.com"), (2, "second@example.com")]:
            connection.execute(
                """
                INSERT INTO users(id, email, password_hash, is_active, created_at, updated_at)
                VALUES (?, ?, 'hash', 1, '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')
                """,
                (user_id, email),
            )
        values = (
            "same-id",
            "openai",
            "response-id",
            1,
            1,
            "2026-01-01T00:00:00+08:00",
            "2026-01-01T00:00:00+08:00",
        )
        connection.execute(
            """
            INSERT INTO conversations(
                user_id, conversation_id, provider, openai_response_id,
                turn_count, state_version, created_at, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        connection.execute(
            """
            INSERT INTO conversations(
                user_id, conversation_id, provider, openai_response_id,
                turn_count, state_version, created_at, updated_at
            ) VALUES (2, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO conversations(
                    user_id, conversation_id, provider, openai_response_id,
                    turn_count, state_version, created_at, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO tasks(
                    user_id, title, priority, status, created_at, updated_at
                ) VALUES (999, 'not owned', 'medium', 'pending',
                '2026-01-01T00:00:00+08:00', '2026-01-01T00:00:00+08:00')
                """
            )

        conversation_indexes = list(connection.execute("PRAGMA index_list(conversations)"))
        unique_indexes = [index for index in conversation_indexes if index[2]]
        assert any(
            [row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})")]
            == ["user_id", "conversation_id"]
            for index in unique_indexes
        )
        assert connection.execute("PRAGMA foreign_key_list(tasks)").fetchone()[2] == "users"
        assert connection.execute("PRAGMA foreign_key_list(conversations)").fetchone()[2] == "users"


def test_repeated_apply_is_rejected_and_backup_can_restore_legacy_schema(tmp_path: Path) -> None:
    database_path = create_legacy_database(tmp_path)
    report = migration.apply(database_path)

    with pytest.raises(migration.MigrationAlreadyAppliedError):
        migration.apply(database_path)

    restored_path = tmp_path / "restored.sqlite"
    shutil.copy2(Path(report.backup_path), restored_path)
    with sqlite3.connect(restored_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert "legacy_tasks" not in table_names(restored_path)
        assert "legacy_conversations" not in table_names(restored_path)


def test_failed_apply_rolls_back_without_renaming_source_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = create_legacy_database(tmp_path)
    original_create_target_tables = migration._create_target_tables

    def fail_after_creating_tables(connection: sqlite3.Connection) -> None:
        original_create_target_tables(connection)
        raise RuntimeError("simulated test failure")

    monkeypatch.setattr(migration, "_create_target_tables", fail_after_creating_tables)

    with pytest.raises(migration.OwnershipMigrationError):
        migration.apply(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
        assert "legacy_tasks" not in table_names(database_path)
        assert "legacy_conversations" not in table_names(database_path)
        assert "schema_migrations" not in table_names(database_path)


@pytest.mark.parametrize("invalid_path", ["", ":memory:", "relative.sqlite", "sqlite:///relative.sqlite"])
def test_migration_requires_an_explicit_existing_absolute_database_path(invalid_path: str) -> None:
    with pytest.raises(migration.OwnershipMigrationError):
        migration.dry_run(invalid_path)
