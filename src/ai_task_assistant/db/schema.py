"""Fail-closed checks for the owner-scoped SQLite schema."""

from sqlalchemy import inspect
from sqlalchemy.engine import Engine


class SchemaCompatibilityError(RuntimeError):
    """Raised before an incompatible legacy database can serve requests."""


def assert_ownership_schema_compatible(engine: Engine) -> None:
    """Reject legacy Task/Conversation tables without altering the database.

    ``create_all`` only creates absent tables. Checking before startup prevents it
    from silently adding unrelated tables to a database that still has the old,
    globally scoped Task or Conversation schema.
    """

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    expected_columns = {
        "tasks": {"id", "user_id", "title", "description", "due_date", "priority", "status", "created_at", "updated_at"},
        "conversations": {
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
        },
    }
    for table_name, required_columns in expected_columns.items():
        if table_name not in tables:
            continue
        columns = {column["name"] for column in inspector.get_columns(table_name)}
        if not required_columns.issubset(columns):
            raise SchemaCompatibilityError(
                "Database schema is incompatible with authenticated ownership. "
                "Run the documented migration before starting the application."
            )

    if "tasks" in tables:
        task_indexes = {index["name"] for index in inspector.get_indexes("tasks")}
        if not {"ix_tasks_user_id_id", "ix_tasks_user_id_due_date"}.issubset(task_indexes):
            raise SchemaCompatibilityError(
                "Database schema is incompatible with authenticated ownership. "
                "Run the documented migration before starting the application."
            )
        if not _has_user_foreign_key(inspector.get_foreign_keys("tasks")):
            raise SchemaCompatibilityError(
                "Database schema is incompatible with authenticated ownership. "
                "Run the documented migration before starting the application."
            )

    if "conversations" in tables:
        conversation_indexes = {
            index["name"] for index in inspector.get_indexes("conversations")
        }
        if "ix_conversations_user_id_updated_at" not in conversation_indexes:
            raise SchemaCompatibilityError(
                "Database schema is incompatible with authenticated ownership. "
                "Run the documented migration before starting the application."
            )
        unique_constraints = inspector.get_unique_constraints("conversations")
        has_scoped_unique = any(
            constraint.get("column_names") == ["user_id", "conversation_id"]
            for constraint in unique_constraints
        )
        if not has_scoped_unique:
            raise SchemaCompatibilityError(
                "Database schema is incompatible with authenticated ownership. "
                "Run the documented migration before starting the application."
            )
        if not _has_user_foreign_key(inspector.get_foreign_keys("conversations")):
            raise SchemaCompatibilityError(
                "Database schema is incompatible with authenticated ownership. "
                "Run the documented migration before starting the application."
            )


def _has_user_foreign_key(foreign_keys: list[dict[str, object]]) -> bool:
    return any(
        foreign_key.get("constrained_columns") == ["user_id"]
        and foreign_key.get("referred_table") == "users"
        and foreign_key.get("referred_columns") == ["id"]
        for foreign_key in foreign_keys
    )
