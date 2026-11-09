"""Бизнес-логика регистрации, входа и обновления токенов."""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ConflictError, UnauthorizedError
from app.core.events import EventSink
from app.core.ratelimit import RateLimiter
from app.core.security import (
    DUMMY_HASH,
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)
from app.modules.users.models import RefreshToken, User, UserRole
from app.modules.users.repository import RefreshTokenRepository, UserRepository
from app.modules.users.schemas import TokenPair

log = structlog.get_logger("users")

# Одно сообщение для «нет такого email» и «неверный пароль»: не раскрываем, какие аккаунты есть.
_BAD_CREDENTIALS = "Неверный email или пароль"
_BAD_REFRESH = "Недействительный refresh-токен"


class AuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        limiter: RateLimiter,
        events: EventSink,
    ) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter
        self._events = events
        self._users = UserRepository(session)
        self._tokens = RefreshTokenRepository(session)

    async def create_user(self, email: str, password: str, role: UserRole) -> User:
        """Создаёт пользователя. Дубль email отсекает UNIQUE в БД, а не проверка «сначала SELECT»:
        проверка-затем-вставка при двух параллельных запросах пропустила бы оба."""
        user = User(email=email, password_hash=await hash_password(password), role=role)
        try:
            await self._users.add(user)
        except IntegrityError:
            await self._session.rollback()
            raise ConflictError("Пользователь с таким email уже существует") from None
        # Событие ставится до commit: с outbox оно попадёт в ту же транзакцию.
        await self._events.publish(
            "user.registered", {"user_id": str(user.id), "email": user.email}
        )
        await self._session.commit()
        return user

    async def register(self, email: str, password: str, client_ip: str) -> User:
        s = self._settings
        await self._limiter.check(
            f"register:ip:{client_ip}", s.register_ip_limit, s.register_window_seconds
        )
        user = await self.create_user(email, password, UserRole.CUSTOMER)
        log.info("user_registered", user_id=str(user.id))
        return user

    async def login(self, email: str, password: str, client_ip: str) -> TokenPair:
        s = self._settings
        email_key = f"login:email:{email}"
        await self._limiter.check(
            f"login:ip:{client_ip}", s.login_ip_limit, s.login_ip_window_seconds
        )
        await self._limiter.ensure_not_blocked(
            email_key, s.login_email_fail_limit, s.login_email_window_seconds
        )

        user = await self._users.get_by_email(email)
        # Пароль проверяем всегда, даже без пользователя: по времени ответа аккаунт не угадать.
        password_ok = await verify_password(user.password_hash if user else DUMMY_HASH, password)
        if user is None or not password_ok or not user.is_active:
            await self._limiter.register_failure(email_key, s.login_email_window_seconds)
            log.warning("login_failed", user_id=str(user.id) if user else None)
            raise UnauthorizedError(_BAD_CREDENTIALS)

        if password_needs_rehash(user.password_hash):
            user.password_hash = await hash_password(password)
        await self._limiter.reset(email_key)
        pair = self._issue_tokens(user, family_id=uuid.uuid4())
        await self._session.commit()
        log.info("login_succeeded", user_id=str(user.id))
        return pair

    async def refresh(self, raw_token: str) -> TokenPair:
        now = datetime.now(UTC)
        token = await self._tokens.get_by_hash(hash_refresh_token(raw_token))
        if token is None:
            raise UnauthorizedError(_BAD_REFRESH)

        # Отозванный токен предъявили снова: либо его украли, либо клиент рассинхронизировался.
        # Безопасный ответ в обоих случаях — закрыть всю цепочку, пользователь войдёт заново.
        # Второе условие ловит гонку: два одновременных запроса с одним токеном, выигрывает один.
        if token.revoked_at is not None or not await self._tokens.revoke_if_active(token.id, now):
            await self._tokens.revoke_family(token.family_id, now)
            await self._session.commit()
            log.warning(
                "refresh_token_reuse_detected",
                user_id=str(token.user_id),
                family_id=str(token.family_id),
            )
            raise UnauthorizedError(_BAD_REFRESH)

        user = await self._users.get_by_id(token.user_id)
        if token.expires_at <= now or user is None or not user.is_active:
            await self._tokens.revoke_family(token.family_id, now)
            await self._session.commit()
            raise UnauthorizedError(_BAD_REFRESH)

        pair = self._issue_tokens(user, family_id=token.family_id)
        await self._session.commit()
        return pair

    async def logout(self, raw_token: str) -> None:
        """Закрывает всю цепочку токенов. Неизвестный токен не ошибка: операция идемпотентна."""
        token = await self._tokens.get_by_hash(hash_refresh_token(raw_token))
        if token is not None:
            await self._tokens.revoke_family(token.family_id, datetime.now(UTC))
            await self._session.commit()

    def _issue_tokens(self, user: User, family_id: uuid.UUID) -> TokenPair:
        s = self._settings
        raw, token_hash = new_refresh_token()
        self._tokens.add(
            RefreshToken(
                user_id=user.id,
                family_id=family_id,
                token_hash=token_hash,
                expires_at=datetime.now(UTC) + timedelta(days=s.refresh_token_ttl_days),
            )
        )
        return TokenPair(
            access_token=create_access_token(user.id, user.role.value, s),
            refresh_token=raw,
            expires_in=s.access_token_ttl_minutes * 60,
        )
