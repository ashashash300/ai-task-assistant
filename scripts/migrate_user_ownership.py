"""Explicit SQLite-only dry-run/apply tool for the future ownership cutover.

This tool is intentionally independent from FastAPI startup and the current ORM.
Do not run apply mode against an application database until the owner-scoped ORM,
services, and routes are deployed together.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3
from typing import Callable

from ai_task_assistant.core.datetime import current_taipei_time


MIGRATION_VERSION = "ownership-v1"
LEGACY_TASKS_TABLE = "legacy_tasks"
LEGACY_CONVERSATIONS_TABLE = "legacy_conversations"

LEGACY_TASK_COLUMNS = {
    "id",
    "title",
    "description",
    "due_date",
    "priority",
    "status",
    "created_at",
    "updated_at",
}
LEGACY_CONVERSATION_COLUMNS = {
    "id",
    "conversation_id",
    "provider",
    "openai_response_id",
    "gemini_history_json",
    "turn_count",
    "state_version",
    "created_at",
    "updated_at",
}


class OwnershipMigrationError(Exception):
    """Raised when ownership migration cannot complete safely."""


class MigrationAlreadyAppliedError(OwnershipMigrationError):
    """Raised when the database already contains this migration version."""


@dataclass(frozen=True)
class MigrationReport:
    mode: str
    database_path: str
    legacy_task_count: int
    legacy_conversation_count: int
    backup_path: str | None
    migration_version: str

    def as_public_dict(self) -> dict[str, object]:
        """Return counts and paths only; never expose legacy rows or credentials."""

        return asdict(self)


def validate_database_path(database_path: str | Path) -> Path:
    """Require an existing, explicit absolute SQLite database file."""

    if not isinstance(database_path, (str, Path)):
        raise OwnershipMigrationError("An explicit SQLite database path is required")
    raw_path = str(database_path).strip()
    if not raw_path or raw_path == ":memory:" or raw_path.startswith("sqlite:"):
        raise OwnershipMigrationError("An explicit SQLite database file path is required")

    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise OwnershipMigrationError("The SQLite database path must be absolute")
    try:
        resolved_path = path.resolve(strict=True)
    except FileNotFoundError as error:
        raise OwnershipMigrationError("The SQLite database file does not exist") from error
    if not resolved_path.is_file():
        raise OwnershipMigrationError("The SQLite database path must identify a file")
    return resolved_path


def _connect(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(database_path))
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _has_global_conversation_id_unique_constraint(connection: sqlite3.Connection) -> bool:
    for index in connection.execute("PRAGMA index_list(conversations)"):
        index_name = index[1]
        is_unique = bool(index[2])
        columns = [row[2] for row in connection.execute(f"PRAGMA index_info({index_name})")]
        if is_unique and columns == ["conversation_id"]:
            return True
    return False


def _integrity_check(connection: sqlite3.Connection) -> None:
    result = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if result != ["ok"]:
        raise OwnershipMigrationError("SQLite integrity check failed")


def _foreign_key_check(connection: sqlite3.Connection) -> None:
    if list(connection.execute("PRAGMA foreign_key_check")):
        raise OwnershipMigrationError("SQLite foreign key check failed")


def _migration_is_applied(connection: sqlite3.Connection) -> bool:
    if "schema_migrations" not in _table_names(connection):
        return False
    return (
        connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (MIGRATION_VERSION,)
        ).fetchone()
        is not None
    )


def _validate_legacy_schema(connection: sqlite3.Connection) -> tuple[int, int]:
    tables = _table_names(connection)
    if _migration_is_applied(connection):
        raise MigrationAlreadyAppliedError("Ownership migration is already applied")
    if LEGACY_TASKS_TABLE in tables or LEGACY_CONVERSATIONS_TABLE in tables:
        raise OwnershipMigrationError("Legacy ownership tables already exist")
    if not {"users", "tasks", "conversations"}.issubset(tables):
        raise OwnershipMigrationError("Required pre-ownership tables are missing")
    if _table_columns(connection, "tasks") != LEGACY_TASK_COLUMNS:
        raise OwnershipMigrationError("Tasks table is not compatible with ownership migration")
    if _table_columns(connection, "conversations") != LEGACY_CONVERSATION_COLUMNS:
        raise OwnershipMigrationError("Conversations table is not compatible with ownership migration")
    if not _has_global_conversation_id_unique_constraint(connection):
        raise OwnershipMigrationError("Conversations table does not have the expected unique constraint")

    _integrity_check(connection)
    _foreign_key_check(connection)
    task_count = connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    conversation_count = connection.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
    return task_count, conversation_count


def _default_backup_path(database_path: Path) -> Path:
    return database_path.with_suffix(f"{database_path.suffix}.pre-ownership-migration.bak")


def _create_backup(database_path: Path, backup_path: Path) -> None:
    if backup_path.exists():
        raise OwnershipMigrationError("Refusing to overwrite an existing migration backup")
    try:
        with sqlite3.connect(str(database_path)) as source, sqlite3.connect(str(backup_path)) as backup:
            source.backup(backup)
        with _connect(backup_path) as backup:
            _integrity_check(backup)
    except sqlite3.Error as error:
        raise OwnershipMigrationError("SQLite backup could not be created safely") from error


def _create_target_tables(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title VARCHAR(255) NOT NULL,
            description TEXT,
            due_date DATETIME,
            priority VARCHAR(6) NOT NULL,
            status VARCHAR(9) NOT NULL,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    connection.execute("CREATE INDEX ix_tasks_user_id_id ON tasks(user_id, id)")
    connection.execute("CREATE INDEX ix_tasks_user_id_due_date ON tasks(user_id, due_date)")
    connection.execute(
        """
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            conversation_id VARCHAR(64) NOT NULL,
            provider VARCHAR(16) NOT NULL,
            openai_response_id VARCHAR(255),
            gemini_history_json JSON,
            turn_count INTEGER NOT NULL DEFAULT 0,
            state_version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            UNIQUE(user_id, conversation_id),
            CONSTRAINT ck_conversations_provider CHECK (provider IN ('openai', 'gemini')),
            CONSTRAINT ck_conversations_turn_count CHECK (turn_count >= 0),
            CONSTRAINT ck_conversations_state_version CHECK (state_version > 0)
        )
        """
    )
    connection.execute(
        "CREATE INDEX ix_conversations_user_id_updated_at ON conversations(user_id, updated_at)"
    )


def dry_run(database_path: str | Path) -> MigrationReport:
    """Validate a source database without writing, backing up, or renaming tables."""

    path = validate_database_path(database_path)
    try:
        with _connect(path) as connection:
            task_count, conversation_count = _validate_legacy_schema(connection)
    except sqlite3.Error as error:
        raise OwnershipMigrationError("SQLite preflight validation failed") from error
    return MigrationReport(
        mode="dry-run",
        database_path=str(path),
        legacy_task_count=task_count,
        legacy_conversation_count=conversation_count,
        backup_path=None,
        migration_version=MIGRATION_VERSION,
    )


def apply(
    database_path: str | Path,
    *,
    backup_path: str | Path | None = None,
    failure_injector: Callable[[], None] | None = None,
) -> MigrationReport:
    """Back up and atomically rebuild only a compatible test/pre-cutover SQLite file."""

    path = validate_database_path(database_path)
    selected_backup_path = (
        _default_backup_path(path)
        if backup_path is None
        else validate_database_path_for_backup(backup_path)
    )

    # Validate before backup, then validate again inside the write transaction.
    dry_run(path)
    _create_backup(path, selected_backup_path)

    try:
        with _connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                task_count, conversation_count = _validate_legacy_schema(connection)
                connection.execute("ALTER TABLE tasks RENAME TO legacy_tasks")
                connection.execute("ALTER TABLE conversations RENAME TO legacy_conversations")
                _create_target_tables(connection)
                if failure_injector is not None:
                    failure_injector()
                connection.execute(
                    """
                    CREATE TABLE schema_migrations (
                        version VARCHAR(64) PRIMARY KEY,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (MIGRATION_VERSION, current_taipei_time().isoformat()),
                )
                _integrity_check(connection)
                _foreign_key_check(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
    except OwnershipMigrationError:
        raise
    except sqlite3.Error as error:
        raise OwnershipMigrationError("Ownership migration could not be applied safely") from error
    except Exception as error:
        raise OwnershipMigrationError("Ownership migration was rolled back") from error

    return MigrationReport(
        mode="apply",
        database_path=str(path),
        legacy_task_count=task_count,
        legacy_conversation_count=conversation_count,
        backup_path=str(selected_backup_path),
        migration_version=MIGRATION_VERSION,
    )


def validate_database_path_for_backup(backup_path: str | Path) -> Path:
    """Require an explicit absolute destination path that does not already exist."""

    if not isinstance(backup_path, (str, Path)):
        raise OwnershipMigrationError("An explicit backup path is required")
    raw_path = str(backup_path).strip()
    if not raw_path:
        raise OwnershipMigrationError("An explicit backup path is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise OwnershipMigrationError("The backup path must be absolute")
    if path.exists():
        raise OwnershipMigrationError("Refusing to overwrite an existing migration backup")
    if not path.parent.is_dir():
        raise OwnershipMigrationError("The backup directory does not exist")
    return path.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run or apply SQLite ownership migration")
    parser.add_argument("--database", required=True, help="Absolute SQLite database file path")
    parser.add_argument("--apply", action="store_true", help="Apply migration; default is dry-run")
    parser.add_argument("--backup", help="Optional absolute backup destination for apply mode")
    arguments = parser.parse_args()

    report = (
        apply(arguments.database, backup_path=arguments.backup)
        if arguments.apply
        else dry_run(arguments.database)
    )
    print(json.dumps(report.as_public_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
