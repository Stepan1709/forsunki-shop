import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from app.core.config import Settings
from app.core.security import (
    JWT_ALGORITHM,
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)

SECRET = "t" * 40  # совпадает с jwt_secret из tests/helpers.py


async def test_password_is_hashed_with_argon2id_and_verified() -> None:
    password_hash = await hash_password("correct horse")

    assert password_hash.startswith("$argon2id$")
    assert "correct horse" not in password_hash
    assert await verify_password(password_hash, "correct horse")
    assert not await verify_password(password_hash, "wrong")


async def test_same_password_gives_different_hashes() -> None:
    # Соль случайна: одинаковые пароли не видны в БД как одинаковые хэши.
    assert await hash_password("same") != await hash_password("same")


async def test_verify_with_garbage_hash_is_false_not_error() -> None:
    assert not await verify_password("not-a-hash", "x")


async def test_fresh_hash_does_not_need_rehash() -> None:
    assert not password_needs_rehash(await hash_password("x"))


def test_access_token_roundtrip(settings: Settings) -> None:
    user_id = uuid.uuid4()

    claims = decode_access_token(create_access_token(user_id, "admin", settings), settings)

    assert claims.user_id == user_id
    assert claims.role == "admin"


def test_expired_token_is_rejected(settings: Settings) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=1)
    token = create_access_token(uuid.uuid4(), "customer", settings, now=long_ago)

    with pytest.raises(TokenError):
        decode_access_token(token, settings)


def test_tampered_token_is_rejected(settings: Settings) -> None:
    token = create_access_token(uuid.uuid4(), "customer", settings)
    header, payload, signature = token.split(".")

    with pytest.raises(TokenError):
        decode_access_token(f"{header}.{payload}.{signature[:-2]}xx", settings)


def test_token_signed_with_other_secret_is_rejected(settings: Settings, tmp_path: Path) -> None:
    other = settings.model_copy(update={"jwt_secret": settings.jwt_secret.__class__("x" * 40)})
    token = create_access_token(uuid.uuid4(), "customer", other)

    with pytest.raises(TokenError):
        decode_access_token(token, settings)


def test_token_with_alg_none_is_rejected(settings: Settings) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "admin",
        "type": "access",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    forged = jwt.encode(payload, key="", algorithm="none")

    with pytest.raises(TokenError):
        decode_access_token(forged, settings)


def test_token_of_other_type_is_rejected(settings: Settings) -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "role": "customer",
        "type": "refresh",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    token = jwt.encode(payload, SECRET, algorithm=JWT_ALGORITHM)

    with pytest.raises(TokenError):
        decode_access_token(token, settings)


def test_refresh_token_is_random_and_stored_as_hash() -> None:
    raw1, hash1 = new_refresh_token()
    raw2, _ = new_refresh_token()

    assert raw1 != raw2
    assert hash1 == hash_refresh_token(raw1)
    assert raw1 not in hash1
    assert len(hash1) == 64
