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
    prepare_conversation,
    save_response,
)


router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse, response_model_exclude_none=True)
def chat(chat_in: ChatRequest, db: Session = Depends(get_db)) -> ChatResponse:
    try:
        conversation_state = (
            prepare_conversation(chat_in.conversation_id) if chat_in.conversation_id else None
        )
        result = run_chat(
            db,
            chat_in.message,
            previous_response_id=(conversation_state.last_response_id if conversation_state else None),
        )
        if chat_in.conversation_id:
            save_response(chat_in.conversation_id, result.response_id)
        return ChatResponse(conversation_id=chat_in.conversation_id, response=result.response)
    except ConversationLimitError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except ConversationContextError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except AIServiceError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
