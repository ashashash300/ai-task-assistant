from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from ai_task_assistant.api.dependencies.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    reject_disallowed_origin,
    require_csrf_protection,
    require_current_user,
)
from ai_task_assistant.core.config import settings
from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.core.security import generate_session_token
from ai_task_assistant.db.session import get_db
from ai_task_assistant.models.user import User
from ai_task_assistant.schemas.auth import (
    CsrfTokenResponse,
    LoginRequest,
    RegisterRequest,
    UserResponse,
)
from ai_task_assistant.services.auth_service import (
    AuthenticationStorageError,
    DuplicateEmailError,
    authenticate_user,
    create_session,
    register_user,
    revoke_session,
)
from ai_task_assistant.services.rate_limit_service import (
    RateLimitExceeded,
    client_ip,
    email_limiter_key,
    rate_limiter,
)


router = APIRouter(prefix="/auth", tags=["auth"])


def _cookie_expiry() -> datetime:
    # Cookie Expires follows HTTP-date, which Starlette requires to be UTC.
    # Database/application timestamps continue to use Asia/Taipei.
    return (current_taipei_time() + timedelta(hours=settings.session_ttl_hours)).astimezone(UTC)


def _set_cookie(response: Response, *, name: str, value: str, httponly: bool) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=settings.session_ttl_hours * 60 * 60,
        expires=_cookie_expiry(),
        path="/",
        secure=settings.session_cookie_secure,
        httponly=httponly,
        samesite=settings.session_cookie_samesite,
    )


def _storage_error_response(error: AuthenticationStorageError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=str(error),
    )


def _rate_limit_response(error: RateLimitExceeded) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=error.detail,
        headers={"Retry-After": str(error.retry_after)},
    )


@router.get("/csrf", response_model=CsrfTokenResponse)
def issue_csrf_token(
    request: Request,
    response: Response,
    _origin: Annotated[str | None, Depends(reject_disallowed_origin)],
) -> CsrfTokenResponse:
    try:
        rate_limiter.consume(
            key=f"csrf:ip:{client_ip(request)}",
            limit=settings.csrf_ip_limit,
            window_seconds=settings.csrf_ip_window_seconds,
        )
    except RateLimitExceeded as error:
        raise _rate_limit_response(error) from error
    csrf_token = generate_session_token()
    _set_cookie(response, name=CSRF_COOKIE_NAME, value=csrf_token, httponly=False)
    return CsrfTokenResponse(csrf_token=csrf_token)


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    request: Request,
    register_in: RegisterRequest,
    _csrf: Annotated[None, Depends(require_csrf_protection)],
    db: Session = Depends(get_db),
) -> UserResponse:
    try:
        rate_limiter.consume(
            key=f"register:ip:{client_ip(request)}",
            limit=settings.register_ip_limit,
            window_seconds=settings.register_ip_window_seconds,
        )
    except RateLimitExceeded as error:
        raise _rate_limit_response(error) from error
    try:
        return register_user(db, email=register_in.email, password=register_in.password)
    except DuplicateEmailError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    except AuthenticationStorageError as error:
        raise _storage_error_response(error) from error


@router.post("/login", response_model=UserResponse)
def login(
    request: Request,
    login_in: LoginRequest,
    response: Response,
    _csrf: Annotated[None, Depends(require_csrf_protection)],
    db: Session = Depends(get_db),
) -> UserResponse:
    try:
        rate_limiter.consume(
            key=f"login:ip:{client_ip(request)}",
            limit=settings.login_ip_limit,
            window_seconds=settings.login_ip_window_seconds,
        )
        rate_limiter.consume(
            key=email_limiter_key(login_in.email),
            limit=settings.login_email_limit,
            window_seconds=settings.login_email_window_seconds,
        )
    except RateLimitExceeded as error:
        raise _rate_limit_response(error) from error
    try:
        user = authenticate_user(db, email=login_in.email, password=login_in.password)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid email or password",
            )
        token, _session = create_session(db, user=user)
    except AuthenticationStorageError as error:
        raise _storage_error_response(error) from error

    _set_cookie(response, name=SESSION_COOKIE_NAME, value=token, httponly=True)
    return user


@router.get("/me", response_model=UserResponse)
def get_current_user(current_user: Annotated[User, Depends(require_current_user)]) -> UserResponse:
    return current_user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    _csrf: Annotated[None, Depends(require_csrf_protection)],
    _current_user: Annotated[User, Depends(require_current_user)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE_NAME)] = None,
    db: Session = Depends(get_db),
) -> Response:
    try:
        revoke_session(db, token=session_token)
    except AuthenticationStorageError as error:
        raise _storage_error_response(error) from error

    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
