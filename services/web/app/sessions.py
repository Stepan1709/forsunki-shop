"""Серверные сессии (паттерн BFF): в cookie лежит только случайный id, токены доступа хранятся в Redis.

Зачем так. Если бы JWT лежал в браузере (localStorage или обычная cookie), любая XSS-уязвимость отдала бы его
атакующему. Здесь браузер видит только непрозрачный id в cookie `HttpOnly`, скрипты страницы его не читают.
Сам токен никогда не покидает сервер.

Детали:
  * id генерируется криптостойко (secrets.token_urlsafe), в Redis ключ строится из SHA-256 от id: даже
    дамп Redis не даёт готовых к использованию значений cookie;
  * TTL продлевается при каждом запросе (скользящее окно);
  * при входе id меняется (rotate): защита от фиксации сессии;
  * Redis недоступен: страницы для гостей продолжают работать, вход невозможен (сессию негде хранить).
"""

import hashlib
import json
import secrets
from http.cookies import SimpleCookie
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings

log = structlog.get_logger("sessions")


def _key(session_id: str) -> str:
    return "web:sess:" + hashlib.sha256(session_id.encode()).hexdigest()


class Session:
    """Данные сессии на время запроса. Изменения сохраняются middleware перед отправкой ответа."""

    def __init__(self, session_id: str, data: dict[str, Any], *, is_new: bool) -> None:
        self.id = session_id
        self.data = data
        self.is_new = is_new
        self.dirty = False
        self.destroyed = False
        self.rotated_from: str | None = None
        self.store_available = True

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value
        self.dirty = True

    def pop(self, key: str, default: Any = None) -> Any:
        if key in self.data:
            self.dirty = True
        return self.data.pop(key, default)

    @property
    def csrf_token(self) -> str:
        token: str = self.data["csrf"]
        return token

    @property
    def is_authenticated(self) -> bool:
        return bool(self.data.get("refresh"))

    def rotate(self) -> None:
        """Новый id и новый CSRF-токен (вызывается при входе)."""
        self.rotated_from = self.rotated_from or self.id
        self.id = secrets.token_urlsafe(32)
        self.data["csrf"] = secrets.token_urlsafe(32)
        self.is_new = True
        self.dirty = True

    def destroy(self) -> None:
        self.destroyed = True
        self.data = {}

    def flash(self, message: str, kind: str = "info") -> None:
        messages = list(self.data.get("flash", []))
        messages.append({"text": message, "kind": kind})
        self.set("flash", messages)

    def pop_flash(self) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = self.pop("flash", [])
        return messages


class SessionStore:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._ttl = settings.session_ttl_seconds
        self._anonymous_ttl = settings.anonymous_session_ttl_seconds

    def _ttl_for(self, data: dict[str, Any]) -> int:
        # Гостевая сессия нужна только ради CSRF-токена форм: держим её недолго, чтобы обход сайта
        # ботами не копил ключи в Redis на неделю.
        return self._ttl if data.get("refresh") else self._anonymous_ttl

    async def load(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(_key(session_id))
        if raw is None:
            return None
        data: dict[str, Any] = json.loads(raw)
        await self._redis.expire(_key(session_id), self._ttl_for(data))  # скользящее окно
        return data

    async def save(self, session_id: str, data: dict[str, Any]) -> None:
        await self._redis.set(
            _key(session_id), json.dumps(data, ensure_ascii=False), ex=self._ttl_for(data)
        )

    async def delete(self, session_id: str) -> None:
        await self._redis.delete(_key(session_id))

    def refresh_lock(self, session_id: str):  # type: ignore[no-untyped-def]
        """Блокировка обновления токенов: два параллельных запроса не должны оба предъявить один refresh-токен
        (api посчитает это кражей и закроет всю цепочку)."""
        return self._redis.lock("web:lock:" + _key(session_id), timeout=15, blocking_timeout=10)


class SessionMiddleware:
    """Чистый ASGI-middleware (а не BaseHTTPMiddleware: тот теряет contextvars с correlation_id)."""

    def __init__(self, app: ASGIApp, store: SessionStore, settings: Settings) -> None:
        self.app = app
        self._store = store
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        s = self._settings
        cookie = SimpleCookie(Headers(scope=scope).get("cookie", ""))
        sid = cookie[s.session_cookie_name].value if s.session_cookie_name in cookie else None

        data: dict[str, Any] | None = None
        available = True
        if sid:
            try:
                data = await self._store.load(sid)
            except (RedisError, ValueError) as exc:
                available = False
                log.warning("session_store_unavailable", error=type(exc).__name__)
        if data is None:
            session = Session(
                secrets.token_urlsafe(32), {"csrf": secrets.token_urlsafe(32)}, is_new=True
            )
        else:
            session = Session(sid or "", data, is_new=False)
        session.store_available = available
        scope.setdefault("state", {})["session"] = session

        async def send_with_session(message: Message) -> None:
            if message["type"] == "http.response.start":
                await self._finalize(session, message)
            await send(message)

        await self.app(scope, receive, send_with_session)

    async def _finalize(self, session: Session, message: Message) -> None:
        s = self._settings
        headers = MutableHeaders(scope=message)
        attrs = f"Path=/; HttpOnly; SameSite=Lax{'; Secure' if s.cookie_secure else ''}"
        try:
            if session.destroyed:
                await self._store.delete(session.id)
                if session.rotated_from:
                    await self._store.delete(session.rotated_from)
                headers.append("set-cookie", f"{s.session_cookie_name}=; Max-Age=0; {attrs}")
                return
            if session.rotated_from:
                await self._store.delete(session.rotated_from)
            # Анонимную сессию без данных, кроме CSRF-токена, храним тоже: форма входа уже содержит токен.
            if session.is_new or session.dirty:
                await self._store.save(session.id, session.data)
            if session.is_new:
                headers.append(
                    "set-cookie",
                    f"{s.session_cookie_name}={session.id}; Max-Age={s.session_ttl_seconds}; {attrs}",
                )
        except RedisError as exc:
            log.warning("session_save_failed", error=type(exc).__name__)
