"""Cookie authentication and CSRF dependencies for future protected routes."""

import secrets
from typing import Annotated

from fastapi import Cookie, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from ai_task_assistant.core.config import settings
from ai_task_assistant.db.session import get_db
from ai_task_assistant.models.user import User
from ai_task_assistant.services.auth_service import get_authenticated_user


SESSION_COOKIE_NAME = "ai_task_session"
CSRF_COOKIE_NAME = "ai_task_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
CSRF_ERROR_CODE = "csrf_validation_failed"


def require_allowed_origin(origin: Annotated[str | None, Header()] = None) -> str:
    if origin is None or origin not in settings.auth_allowed_origin_values:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request origin is not allowed")
    return origin


def require_csrf_protection(
    _origin: Annotated[str, Depends(require_allowed_origin)],
    csrf_cookie: Annotated[str | None, Cookie(alias=CSRF_COOKIE_NAME)] = None,
    csrf_header: Annotated[str | None, Header(alias=CSRF_HEADER_NAME)] = None,
) -> None:
    if not csrf_cookie or not csrf_header or not secrets.compare_digest(csrf_cookie, csrf_header):
        # This dependency runs before the endpoint body. The frontend may safely
        # fetch one fresh CSRF token and retry only this explicitly coded failure.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": CSRF_ERROR_CODE, "message": "CSRF validation failed"},
        )


def require_current_user(
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    db: Session = Depends(get_db),
) -> User:
    user = get_authenticated_user(db, token=session_token)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user
