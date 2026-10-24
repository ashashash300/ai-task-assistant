"""Deterministic, tool-calling provider used only by the local interview demo."""

import json
from typing import Any

from ai_task_assistant.services.llm_provider import ProviderContextError, ProviderResult, ToolExecutor


CREATE_DEMO_TASK_COMMAND = "建立面試準備任務"
COMPLETE_DEMO_TASK_COMMAND = "完成剛剛的任務"
DEMO_TASK_TITLE = "準備 AI 面試"
DEMO_TASK_DESCRIPTION = "由 deterministic interview demo 建立的任務。"
_DEMO_RESPONSE_STATE = "deterministic-demo-response"


class DeterministicDemoProvider:
    """A narrow provider that exercises the existing validated tool callback.

    This class deliberately recognizes only two exact Chinese commands. It has
    no database dependency and cannot issue arbitrary or destructive tools.
    """

    def run_turn(
        self,
        *,
        message: str,
        system_prompt: str,
        conversation_state: object | None,
        execute_tool: ToolExecutor,
    ) -> ProviderResult:
        del system_prompt
        if conversation_state is not None and not isinstance(conversation_state, str):
            raise ProviderContextError("Conversation context is unavailable. Start a new conversation_id.")

        normalized_message = message.strip()
        if normalized_message == CREATE_DEMO_TASK_COMMAND:
            return self._create_task(execute_tool)
        if normalized_message == COMPLETE_DEMO_TASK_COMMAND:
            return self._complete_latest_demo_task(execute_tool)
        return ProviderResult(
            response=(
                "這是 deterministic demo。請輸入「建立面試準備任務」或「完成剛剛的任務」。"
            ),
            conversation_state=_DEMO_RESPONSE_STATE,
        )

    @staticmethod
    def _create_task(execute_tool: ToolExecutor) -> ProviderResult:
        result = execute_tool(
            "create_task",
            json.dumps(
                {
                    "title": DEMO_TASK_TITLE,
                    "description": DEMO_TASK_DESCRIPTION,
                    "priority": "high",
                },
                ensure_ascii=False,
            ),
        )
        if isinstance(result, dict) and result.get("ok") is True:
            return ProviderResult(
                response="已建立「準備 AI 面試」任務，優先順序為高。",
                conversation_state=_DEMO_RESPONSE_STATE,
            )
        return ProviderResult(
            response="無法建立 demo 任務，請重新開始本次 demo。",
            conversation_state=_DEMO_RESPONSE_STATE,
        )

    @staticmethod
    def _complete_latest_demo_task(execute_tool: ToolExecutor) -> ProviderResult:
        listed = execute_tool("list_tasks", "{}")
        tasks = listed.get("tasks") if isinstance(listed, dict) and listed.get("ok") is True else None
        if not isinstance(tasks, list):
            return ProviderResult(
                response="無法讀取 demo 任務，請重新開始本次 demo。",
                conversation_state=_DEMO_RESPONSE_STATE,
            )

        # The list tool is already scoped to the authenticated user. Of the
        # tasks created by this provider, complete the newest pending row.
        candidates = [
            task
            for task in tasks
            if isinstance(task, dict)
            and task.get("title") == DEMO_TASK_TITLE
            and task.get("description") == DEMO_TASK_DESCRIPTION
            and task.get("status") == "pending"
            and isinstance(task.get("id"), int)
        ]
        if not candidates:
            return ProviderResult(
                response="找不到可完成的 demo 任務。請先輸入「建立面試準備任務」。",
                conversation_state=_DEMO_RESPONSE_STATE,
            )

        selected_task = max(candidates, key=lambda task: task["id"])
        result = execute_tool(
            "complete_task",
            json.dumps({"task_id": selected_task["id"]}, ensure_ascii=False),
        )
        if isinstance(result, dict) and result.get("ok") is True:
            return ProviderResult(
                response="已完成剛剛建立的「準備 AI 面試」任務。",
                conversation_state=_DEMO_RESPONSE_STATE,
            )
        return ProviderResult(
            response="無法完成 demo 任務，請重新開始本次 demo。",
            conversation_state=_DEMO_RESPONSE_STATE,
        )
