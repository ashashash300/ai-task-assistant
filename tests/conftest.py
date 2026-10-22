import pytest

from ai_task_assistant.services.rate_limit_service import clear_rate_limits


@pytest.fixture(autouse=True)
def reset_process_local_rate_limits():
    """Keep test cases independent of the single-process limiter state."""

    clear_rate_limits()
    yield
    clear_rate_limits()
