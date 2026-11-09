"""Пароли, JWT и refresh-токены. Только криптография и разбор токенов, без обращений к БД."""

import asyncio
import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.core.config import Settings

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access"  # noqa: S105  (тип токена в claims, не пароль)

# Argon2id с параметрами по умолчанию библиотеки (рекомендации RFC 9106 для низкой памяти).
_hasher = PasswordHasher()
# Хэш от случайного пароля: при входе с неизвестным email проверяем пароль против него, чтобы
# время ответа не выдавало, существует ли такой аккаунт.
DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


# Хэширование занимает десятки миллисекунд CPU. В event loop оно заблокировало бы все запросы,
# поэтому выполняем в потоке (argon2-cffi отпускает GIL на время вычисления).
async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(password_hash: str, password: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """True, если хэш создан со старыми параметрами: пересчитываем при удачном входе."""
    return _hasher.check_needs_rehash(password_hash)


class TokenError(Exception):
    """Токен подделан, просрочен или не того типа."""


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    role: str


def create_access_token(
    user_id: uuid.UUID, role: str, settings: Settings, now: datetime | None = None
) -> str:
    issued_at = now or datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": ACCESS_TOKEN_TYPE,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=settings.access_token_ttl_minutes),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str, settings: Settings) -> AccessClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret.get_secret_value(),
            # Алгоритм задаём явно: иначе токен с alg=none или чужим алгоритмом мог бы пройти.
            algorithms=[JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
        if payload.get("type") != ACCESS_TOKEN_TYPE:
            raise TokenError("неверный тип токена")
        return AccessClaims(user_id=uuid.UUID(payload["sub"]), role=str(payload["role"]))
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise TokenError("недействительный токен") from exc


def new_refresh_token() -> tuple[str, str]:
    """Возвращает (токен для клиента, хэш для БД). В БД сырой токен не хранится."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    # У токена 256 бит энтропии, подбор невозможен, поэтому медленный Argon2 не нужен:
    # быстрый SHA-256 позволяет искать токен в БД по индексу.
    return hashlib.sha256(raw.encode()).hexdigest()
