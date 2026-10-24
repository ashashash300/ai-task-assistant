from ai_task_assistant.core.security import (
    generate_session_token,
    hash_password,
    hash_session_token,
    normalize_email,
    verify_password,
)


def test_normalize_email_trims_and_casefolds() -> None:
    assert normalize_email("  User.Name+tag@Example.COM  ") == "user.name+tag@example.com"


def test_password_hash_is_not_the_password_and_verifies() -> None:
    password = "correct horse battery staple"
    password_hash = hash_password(password)

    assert password_hash != password
    assert verify_password(password, password_hash) is True
    assert verify_password("incorrect password", password_hash) is False


def test_password_hashes_use_distinct_random_salts() -> None:
    password = "same password"

    assert hash_password(password) != hash_password(password)


def test_malformed_password_hash_fails_safely() -> None:
    assert verify_password("password", "not-a-valid-argon2-hash") is False
    assert verify_password("password", "") is False


def test_session_tokens_are_distinct_and_have_256_bit_source_entropy() -> None:
    first_token = generate_session_token()
    second_token = generate_session_token()

    assert first_token != second_token
    assert len(first_token) >= 43
    assert len(second_token) >= 43


def test_session_token_hash_is_stable_and_does_not_contain_the_token() -> None:
    token = generate_session_token()

    token_hash = hash_session_token(token)

    assert token_hash == hash_session_token(token)
    assert token_hash != token
    assert token not in token_hash
    assert len(token_hash) == 64
