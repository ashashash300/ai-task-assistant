from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ai_task_assistant.db.session import get_db
from ai_task_assistant.schemas.chat import ChatRequest, ChatResponse
from ai_task_assistant.services.ai_service import (
    AIServiceError,
    ConversationContextError,
    run_chat,
)
from ai_task_assistant.services.conversation_service import (
    ConversationLimitError,
    ConversationProviderMismatchError,
    clear_pending_deletion,
    get_pending_deletion,
    prepare_conversation,
    save_pending_deletion,
    save_response,
)
from ai_task_assistant.core.config import settings
from ai_task_assistant.services import task_service


router = APIRouter(tags=["chat"])


CONFIRM_DELETION_MESSAGES = {"確認", "確認刪除", "確定", "是", "yes", "y"}
CANCEL_DELETION_MESSAGES = {"取消", "取消刪除", "不要", "否", "no", "n"}
DELETE_INTENT_MARKERS = ("刪除", "刪掉", "移除", "delete", "remove")


def _normalized_message(message: str) -> str:
    return message.strip().casefold()


def _is_delete_intent(message: str) -> bool:
    return any(marker in _normalized_message(message) for marker in DELETE_INTENT_MARKERS)


@router.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
def chat(chat_in: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    try:
        conversation_state = (
            prepare_conversation(chat_in.conversation_id, settings.ai_provider)
            if chat_in.conversation_id
            else None
        )
        if chat_in.conversation_id:
            pending_deletion = get_pending_deletion(chat_in.conversation_id)
            normalized_message = _normalized_message(chat_in.message)
            if pending_deletion and normalized_message in CANCEL_DELETION_MESSAGES:
                clear_pending_deletion(chat_in.conversation_id)
                return ChatResponse(
                    conversation_id=chat_in.conversation_id,
                    response=f"已取消刪除「{pending_deletion.title}」。",
                )
            if pending_deletion and normalized_message in CONFIRM_DELETION_MESSAGES:
                # Consume before deletion so the same confirmation can never be reused.
                clear_pending_deletion(chat_in.conversation_id)
                task = task_service.get_task(db, pending_deletion.task_id)
                if task is None or task.title != pending_deletion.title:
                    return ChatResponse(
                        conversation_id=chat_in.conversation_id,
                        response="待刪除任務已變更或不存在，未執行刪除。",
                    )
                task_service.delete_task(db, task)
                return ChatResponse(
                    conversation_id=chat_in.conversation_id,
                    response=f"已刪除「{pending_deletion.title}」。",
                )
            if pending_deletion and _is_delete_intent(chat_in.message):
                # A new deletion request must never reuse an earlier confirmation.
                clear_pending_deletion(chat_in.conversation_id)
        result = run_chat(
            db,
            chat_in.message,
            conversation_state=(conversation_state.provider_state if conversation_state else None),
            conversation_id=chat_in.conversation_id,
        )
        if chat_in.conversation_id:
            save_response(chat_in.conversation_id, result.conversation_state, settings.ai_provider)
            if result.pending_deletion:
                save_pending_deletion(chat_in.conversation_id, result.pending_deletion)
                return ChatResponse(
                    conversation_id=chat_in.conversation_id,
                    response=f"請確認是否刪除「{result.pending_deletion.title}」？",
                )
        return ChatResponse(conversation_id=chat_in.conversation_id, response=result.response)
    except ConversationLimitError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ConversationContextError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ConversationProviderMismatchError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except AIServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
