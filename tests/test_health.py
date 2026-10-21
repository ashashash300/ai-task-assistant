from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import ai_task_assistant.main as main_module
from ai_task_assistant.main import app


def test_health_check() -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    original_engine = main_module.engine
    main_module.engine = test_engine
    try:
        with TestClient(app) as client:
            response = client.get("/health")
    finally:
        main_module.engine = original_engine
        test_engine.dispose()

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
