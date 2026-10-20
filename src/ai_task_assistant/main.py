from contextlib import asynccontextmanager

from fastapi import FastAPI

from ai_task_assistant.api.routes import chat_router, tasks_router
from ai_task_assistant.db.base import Base
from ai_task_assistant.db.session import engine
import ai_task_assistant.models  # Register SQLAlchemy models before creating tables.


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="AI Task Assistant", lifespan=lifespan)
app.include_router(tasks_router)
app.include_router(chat_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok"}
