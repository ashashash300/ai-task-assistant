from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ai_task_assistant.core.config import settings


engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    """Provide a database session for one request."""

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
