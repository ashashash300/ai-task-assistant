"""Authentication persistence helpers without HTTP or cookie concerns."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ai_task_assistant.core.config import settings
from ai_task_assistant.core.datetime import current_taipei_time
from ai_task_assistant.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    normalize_email,
    verify_password,
)
from ai_task_assistant.models.session import UserSession
from ai_task_assistant.models.user import User


class DuplicateEmailError(Exception):
    """Raised when a normalized email is already registered."""


class AuthenticationStorageError(Exception):
    """Raised when auth state cannot be safely persisted."""


# This has the same Argon2id work profile as a real account check and avoids an
# obvious timing difference when a submitted email is not registered.
_DUMMY_PASSWORD_HASH = hash_password("not-a-user-password")


def register_user(db: Session, *, email: str, password: str) -> User:
    user = User(email=normalize_email(email), password_hash=hash_password(password))
    try:
        db.add(user)
        db.commit()
        db.refresh(user)
    except IntegrityError as error:
        db.rollback()
        raise DuplicateEmailError("Unable to create account with this email") from error
    except SQLAlchemyError as error:
        db.rollback()
        raise AuthenticationStorageError("Authentication is temporarily unavailable") from error
    return user


def authenticate_user(db: Session, *, email: str, password: str) -> User | None:
    normalized_email = normalize_email(email)
    try:
        user = db.scalar(select(User).where(User.email == normalized_email))
    except SQLAlchemyError as error:
        raise AuthenticationStorageError("Authentication is temporarily unavailable") from error

    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_matches = verify_password(password, password_hash)
    if user is None or not user.is_active or not password_matches:
        return None
    return user


def create_session(db: Session, *, user: User) -> tuple[str, UserSession]:
    token = generate_session_token()
    now = current_taipei_time()
    session = UserSession(
        user_id=user.id,
        token_hash=hash_session_token(token),
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
        created_at=now,
        last_seen_at=now,
    )
    try:
        db.add(session)
        db.commit()
        db.refresh(session)
    except IntegrityError as error:
        db.rollback()
        raise AuthenticationStorageError("Authentication is temporarily unavailable") from error
    except SQLAlchemyError as error:
        db.rollback()
        raise AuthenticationStorageError("Authentication is temporarily unavailable") from error
    return token, session


def get_authenticated_user(db: Session, *, token: str | None) -> User | None:
    if not token:
        return None
    try:
        session = db.scalar(select(UserSession).where(UserSession.token_hash == hash_session_token(token)))
        if session is None or session.revoked_at is not None:
            return None
        if session.expires_at <= current_taipei_time():
            return None
        user = db.get(User, session.user_id)
    except (SQLAlchemyError, ValueError):
        return None
    if user is None or not user.is_active:
        return None
    return user


def revoke_session(db: Session, *, token: str | None) -> None:
    if not token:
        return
    try:
        session = db.scalar(select(UserSession).where(UserSession.token_hash == hash_session_token(token)))
        if session is None or session.revoked_at is not None:
            return
        session.revoked_at = current_taipei_time()
        db.commit()
    except (SQLAlchemyError, ValueError) as error:
        db.rollback()
        raise AuthenticationStorageError("Authentication is temporarily unavailable") from error
