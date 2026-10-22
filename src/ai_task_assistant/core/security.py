"""Small, side-effect-free helpers for future authentication routes."""

import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type


SESSION_TOKEN_BYTES = 32

_password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65_536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def normalize_email(email: str) -> str:
    """Apply the project's conservative email normalization policy."""

    if not isinstance(email, str):
        raise TypeError("email must be a string")
    return email.strip().casefold()


def hash_password(password: str) -> str:
    """Hash a non-empty password with Argon2id and a per-password random salt."""

    if not isinstance(password, str) or not password:
        raise ValueError("password must not be empty")
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Safely verify a password without exposing malformed hash details."""

    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False
    try:
        return _password_hasher.verify(password_hash, password)
    except (InvalidHashError, VerificationError, TypeError, ValueError):
        return False


def generate_session_token() -> str:
    """Generate an opaque token backed by at least 256 bits of randomness."""

    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """Return a stable SHA-256 digest for a high-entropy opaque session token."""

    if not isinstance(token, str) or not token:
        raise ValueError("session token must not be empty")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
