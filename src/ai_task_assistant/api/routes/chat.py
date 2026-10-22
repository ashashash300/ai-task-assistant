from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ai_task_assistant.api.dependencies.auth import (
    require_csrf_protection,
    require_current_user,
)
from ai_task_assistant.core.config import settings
from ai_task_assistant.db.session import get_db
from ai_task_assistant.models.user import User
from ai_task_assistant.schemas.chat import ChatRequest, ChatResponse
from ai_task_assistant.services.ai_service import (
    AIServiceError,
    ConversationContextError,
    run_chat,
)
from ai_task_assistant.services.conversation_service import (
    ConversationConflictError,
    ConversationLimitError,
    ConversationProviderMismatchError,
    ConversationStateError,
    ConversationStorageError,
    clear_pending_deletion,
    consume_pending_deletion,
    get_pending_deletion,
    prepare_conversation,
    save_pending_deletion,
    save_response,
)
from ai_task_assistant.services import task_service
from ai_task_assistant.services.chat_quota_service import (
    ChatQuotaExceeded,
    ChatQuotaStorageError,
    reserve_daily_chat_quota,
)
from ai_task_assistant.services.rate_limit_service import RateLimitExceeded, rate_limiter


router = APIRouter(tags=["chat"])


CONFIRM_DELETION_MESSAGES = {"確認", "確認刪除", "確定", "是", "yes", "y"}
CANCEL_DELETION_MESSAGES = {"取消", "取消刪除", "不要", "否", "no", "n"}
DELETE_INTENT_MARKERS = ("刪除", "刪掉", "移除", "delete", "remove")


def _normalized_message(message: str) -> str:
    return message.strip().casefold()


def _is_delete_intent(message: str) -> bool:
    return any(marker in _normalized_message(message) for marker in DELETE_INTENT_MARKERS)


@router.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
def chat(
    chat_in: ChatRequest,
    current_user: User = Depends(require_current_user),
    _csrf: None = Depends(require_csrf_protection),
    db: Session = Depends(get_db),
) -> ChatResponse:
    admitted = False
    try:
        try:
            rate_limiter.acquire_chat(
                user_id=current_user.id,
                minute_limit=settings.chat_user_limit,
                minute_window_seconds=settings.chat_user_window_seconds,
                concurrent_limit=settings.chat_concurrent_limit,
            )
            admitted = True
            # Commit the durable reservation before any provider call. A provider
            # failure still consumes a request allowance by design.
            reserve_daily_chat_quota(
                db,
                user_id=current_user.id,
                daily_limit=settings.chat_daily_quota,
            )
        except RateLimitExceeded as error:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=error.detail,
                headers={"Retry-After": str(error.retry_after)},
            ) from error
        except ChatQuotaExceeded as error:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Chat quota exceeded",
                headers={"Retry-After": str(error.retry_after)},
            ) from error
        except ChatQuotaStorageError as error:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Chat quota is temporarily unavailable",
            ) from error
        conversation_state = (
            prepare_conversation(
                db,
                user_id=current_user.id,
                conversation_id=chat_in.conversation_id,
                provider=settings.ai_provider,
            )
            if chat_in.conversation_id
            else None
        )
        # Release the read-only transaction before calling an external LLM provider.
        db.rollback()
        if chat_in.conversation_id:
            pending_deletion = get_pending_deletion(
                user_id=current_user.id,
                conversation_id=chat_in.conversation_id,
            )
            normalized_message = _normalized_message(chat_in.message)
            if pending_deletion and normalized_message in CANCEL_DELETION_MESSAGES:
                clear_pending_deletion(user_id=current_user.id, conversation_id=chat_in.conversation_id)
                return ChatResponse(
                    conversation_id=chat_in.conversation_id,
                    response=f"已取消刪除「{pending_deletion.title}」。",
                )
            if pending_deletion and normalized_message in CONFIRM_DELETION_MESSAGES:
                # Atomically consume before deletion so concurrent confirmations
                # cannot reuse the same process-local authorization.
                pending_deletion = consume_pending_deletion(
                    user_id=current_user.id,
                    conversation_id=chat_in.conversation_id,
                )
                if pending_deletion is None:
                    return ChatResponse(
                        conversation_id=chat_in.conversation_id,
                        response="沒有待確認的刪除要求。",
                    )
                task = task_service.get_task(
                    db, user_id=current_user.id, task_id=pending_deletion.task_id
                )
                if task is None or task.title != pending_deletion.title:
                    return ChatResponse(
                        conversation_id=chat_in.conversation_id,
                        response="待刪除任務已變更或不存在，未執行刪除。",
                    )
                task_service.delete_task(
                    db, user_id=current_user.id, task_id=pending_deletion.task_id
                )
                return ChatResponse(
                    conversation_id=chat_in.conversation_id,
                    response=f"已刪除「{pending_deletion.title}」。",
                )
            if pending_deletion and _is_delete_intent(chat_in.message):
                # A new deletion request must never reuse an earlier confirmation.
                clear_pending_deletion(user_id=current_user.id, conversation_id=chat_in.conversation_id)
        result = run_chat(
            db,
            chat_in.message,
            user_id=current_user.id,
            conversation_state=(conversation_state.provider_state if conversation_state else None),
            conversation_id=chat_in.conversation_id,
        )
        if chat_in.conversation_id:
            save_response(
                db,
                user_id=current_user.id,
                conversation_id=chat_in.conversation_id,
                provider_state=result.conversation_state,
                provider=settings.ai_provider,
                expected_state_version=(
                    conversation_state.state_version if conversation_state else None
                ),
                expected_row_id=(conversation_state.row_id if conversation_state else None),
            )
            if result.pending_deletion:
                save_pending_deletion(
                    user_id=current_user.id,
                    conversation_id=chat_in.conversation_id,
                    request=result.pending_deletion,
                )
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
    except (ConversationStateError, ConversationConflictError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ConversationStorageError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    except AIServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
    finally:
        if admitted:
            rate_limiter.release_chat(user_id=current_user.id)
