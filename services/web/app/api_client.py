"""Клиент бэкенда `api`: один общий httpx.AsyncClient на процесс, токены берутся из сессии пользователя.

Что делает сверх обычного HTTP-вызова:
  * передаёт X-Request-ID, поэтому один correlation_id виден в логах сайта, api и воркера;
  * подставляет access-токен пользователя и сам обновляет его по refresh-токену (один раз), если он истёк;
  * переводит ответы Problem Details в `ApiError` с текстом, который можно показать человеку;
  * сетевой сбой превращает в `ApiUnavailable`, чтобы сайт показал понятную страницу, а не трассировку.
"""

import time
from typing import Any

import httpx
import structlog
from redis.exceptions import RedisError

from app.clientip import current_client_ip
from app.logging import CORRELATION_HEADER, current_correlation_id
from app.sessions import Session, SessionStore

log = structlog.get_logger("api_client")

# Обновляем токен заранее: запрос не должен упасть на границе срока жизни.
_REFRESH_MARGIN_SECONDS = 20


class ApiError(Exception):
    def __init__(
        self,
        status: int,
        detail: str,
        *,
        field_errors: dict[str, str] | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail
        self.field_errors = field_errors or {}
        self.retry_after = retry_after


class ApiUnavailable(Exception):
    """api недоступен или не ответил вовремя."""


class AuthRequired(Exception):
    """Нужен вход: токенов нет или обновить их не удалось. Обработчик перенаправляет на страницу входа."""


def _to_error(response: httpx.Response) -> ApiError:
    try:
        body = response.json()
    except ValueError:
        body = {}
    detail = str(body.get("detail") or "Не удалось выполнить запрос")
    field_errors: dict[str, str] = {}
    for item in body.get("errors") or []:
        loc = [str(p) for p in item.get("loc", []) if p not in ("body", "query")]
        if loc:
            field_errors.setdefault(loc[-1], str(item.get("msg", "")))
    retry = response.headers.get("Retry-After")
    return ApiError(
        response.status_code,
        detail,
        field_errors=field_errors,
        retry_after=int(retry) if retry and retry.isdigit() else None,
    )


class ApiClient:
    def __init__(self, http: httpx.AsyncClient, session: Session, store: SessionStore) -> None:
        self._http = http
        self._session = session
        self._store = store

    # ------------------------------------------------------------------------------- вызовы
    async def request(
        self,
        method: str,
        path: str,
        *,
        auth: bool = False,
        json: Any = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        if auth:
            await self._ensure_fresh_token()
        response = await self._send(method, path, auth, json, data, files, params, headers)
        if response.status_code == 401 and auth and self._session.get("refresh"):
            # Токен мог истечь или быть отозван: одна попытка обновления и один повтор запроса.
            await self._refresh(force=True)
            response = await self._send(method, path, auth, json, data, files, params, headers)
        if response.status_code == 401 and auth:
            self._drop_tokens()
            raise AuthRequired
        if response.status_code >= 400:
            raise _to_error(response)
        return response

    async def get(self, path: str, **kwargs: Any) -> Any:
        return (await self.request("GET", path, **kwargs)).json()

    async def post(self, path: str, **kwargs: Any) -> Any:
        response = await self.request("POST", path, **kwargs)
        return response.json() if response.content else None

    async def put(self, path: str, **kwargs: Any) -> Any:
        response = await self.request("PUT", path, **kwargs)
        return response.json() if response.content else None

    async def patch(self, path: str, **kwargs: Any) -> Any:
        response = await self.request("PATCH", path, **kwargs)
        return response.json() if response.content else None

    async def delete(self, path: str, **kwargs: Any) -> None:
        await self.request("DELETE", path, **kwargs)

    # ------------------------------------------------------------------------------- вход
    async def login(self, email: str, password: str) -> None:
        tokens = await self.post("/auth/login", json={"email": email, "password": password})
        self._store_tokens(tokens)
        me = await self.get("/users/me", auth=True)
        self._session.set("user", {"id": me["id"], "email": me["email"], "role": me["role"]})

    async def logout(self) -> None:
        refresh = self._session.get("refresh")
        if refresh:
            try:
                await self.request("POST", "/auth/logout", json={"refresh_token": refresh})
            except (ApiError, ApiUnavailable):
                log.warning("api_logout_failed")  # сессию всё равно удаляем: пользователь вышел

    # ------------------------------------------------------------------------------- внутреннее
    async def _send(
        self,
        method: str,
        path: str,
        auth: bool,
        json: Any,
        data: dict[str, Any] | None,
        files: dict[str, Any] | None,
        params: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        merged = dict(headers or {})
        correlation_id = current_correlation_id()
        if correlation_id:
            merged[CORRELATION_HEADER] = correlation_id
        client_ip = current_client_ip()
        if client_ip:
            # api считает лимиты по адресу клиента; веб-сервер для него доверенный прокси (см. clientip.py).
            merged["X-Forwarded-For"] = client_ip
        if auth and self._session.get("access"):
            merged["Authorization"] = f"Bearer {self._session.get('access')}"
        try:
            return await self._http.request(
                method, path, json=json, data=data, files=files, params=params, headers=merged
            )
        except httpx.HTTPError as exc:
            log.warning("api_unreachable", path=path, error=type(exc).__name__)
            raise ApiUnavailable from exc

    def _store_tokens(self, tokens: dict[str, Any]) -> None:
        self._session.set("access", tokens["access_token"])
        self._session.set("refresh", tokens["refresh_token"])
        self._session.set("access_exp", time.time() + int(tokens["expires_in"]))

    def _drop_tokens(self) -> None:
        for key in ("access", "refresh", "access_exp", "user"):
            self._session.pop(key)

    async def _ensure_fresh_token(self) -> None:
        if not self._session.get("refresh"):
            raise AuthRequired
        if time.time() >= float(self._session.get("access_exp", 0)) - _REFRESH_MARGIN_SECONDS:
            await self._refresh(force=False)

    async def _refresh(self, *, force: bool) -> None:
        """Обновляет токены под блокировкой.

        refresh-токен одноразовый: если два параллельных запроса предъявят один и тот же, api решит, что токен
        украден, и закроет всю цепочку. Поэтому под замком сначала перечитывается сессия: возможно, токены уже
        обновил соседний запрос, тогда достаточно взять новые.
        """
        stale_refresh = self._session.get("refresh")
        try:
            async with self._store.refresh_lock(self._session.id):
                latest = await self._store.load(self._session.id)
                if latest and latest.get("refresh") and latest.get("refresh") != stale_refresh:
                    for key in ("access", "refresh", "access_exp"):
                        self._session.data[key] = latest[key]
                    self._session.dirty = True
                    return
                if not force and time.time() < float(latest.get("access_exp", 0) if latest else 0):
                    return
                try:
                    tokens = await self.post(
                        "/auth/refresh", json={"refresh_token": self._session.get("refresh")}
                    )
                except ApiError as exc:
                    log.info("token_refresh_rejected", status=exc.status)
                    self._drop_tokens()
                    raise AuthRequired from exc
                self._store_tokens(tokens)
                # Сразу в Redis, не дожидаясь конца запроса: соседние запросы должны увидеть новые токены.
                await self._store.save(self._session.id, self._session.data)
        except RedisError as exc:
            log.warning("token_refresh_lock_failed", error=type(exc).__name__)
            raise ApiUnavailable from exc
