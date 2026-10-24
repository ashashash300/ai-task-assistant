from sqlalchemy import event, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ai_task_assistant.core.config import settings


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """Ensure SQLite foreign-key constraints are enabled for every SQLite engine."""

    if dbapi_connection.__class__.__module__.split(".")[0] != "sqlite3":
        return

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """Provide a database session for one request."""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
