"""Local-only backend entry point for browser E2E tests.

It is intentionally separate from the production startup command. Playwright
sets an isolated SQLite URL and disables dotenv loading before this script runs.
"""

import os

if os.getenv("AI_TASK_ASSISTANT_E2E") != "1":
    raise SystemExit("This test-only server requires AI_TASK_ASSISTANT_E2E=1")
if not os.getenv("AI_TASK_ASSISTANT_E2E_INSTANCE_ID"):
    raise SystemExit("This test-only server requires an explicit E2E instance identifier")

from ai_task_assistant.services import ai_service
from ai_task_assistant.services.llm_provider import ProviderResult


class BrowserE2EMockProvider:
    """Never contacts an external AI provider during browser tests."""

    def run_turn(self, **_kwargs) -> ProviderResult:
        return ProviderResult(response="Mock AI response", conversation_state="browser-e2e-response")


ai_service.get_provider = lambda: BrowserE2EMockProvider()

from ai_task_assistant.main import app  # noqa: E402


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")
