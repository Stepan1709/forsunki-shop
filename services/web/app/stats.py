"""Подсчёт просмотров для страницы «Статистика» в админке.

Сайт при каждом просмотре увеличивает счётчики в Redis (одна быстрая команда, без записи в БД):
  * `stats:views:<день>`    сколько страниц открыли за день;
  * `stats:product:<день>`  хеш «id товара -> сколько раз открыли его карточку».
`<день>` это дата ГГГГ-ММ-ДД по московскому времени. Воркер `api` раз в час переносит накопленное в Postgres
(см. api/app/worker/stats_flush.py), формат ключей это контракт между сервисами.

Что НЕ считается: запросы не за страницей (статика, проверки здоровья, метрики), частичные обновления HTMX
(«Показать ещё», смена фильтра: пользователь остаётся на той же странице), страницы админки, боты, пустой
User-Agent и сами администраторы (их просмотры исказили бы картину покупателей). Сбой Redis не должен
ломать страницу: счётчик просто пропускается.
"""

import re
from datetime import datetime, timedelta, timezone

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.deps import current_user

log = structlog.get_logger("stats")

# День по московскому времени (в Москве нет перехода на летнее время, поэтому достаточно фиксированного сдвига).
_MSK = timezone(timedelta(hours=3))

VIEWS_PREFIX = "stats:views:"
PRODUCT_PREFIX = "stats:product:"

# Пути, которые не являются страницами для посетителя.
_IGNORED_PREFIXES = ("/static", "/health", "/metrics", "/admin")

# Грубый, но достаточный признак бота: честные поисковые роботы и скрипты называют себя в User-Agent.
_BOT = re.compile(
    r"bot|crawl|spider|slurp|curl|wget|python|httpx|requests|java|go-http|monitor|uptime|probe|"
    r"headless|preview|scanner|facebookexternalhit|lighthouse",
    re.IGNORECASE,
)


def today_key() -> str:
    return datetime.now(_MSK).date().isoformat()


def is_bot(user_agent: str | None) -> bool:
    """Пустой User-Agent считаем ботом: настоящие браузеры его всегда присылают."""
    return not user_agent or _BOT.search(user_agent) is not None


def _is_countable_visitor(headers: dict[str, str], session: object) -> bool:
    if "hx-request" in headers or is_bot(headers.get("user-agent")):
        return False
    user = current_user(session) if session is not None else None  # type: ignore[arg-type]
    return not (user is not None and user.is_admin)


async def _incr(redis: Redis, command: str, key: str, field: str | None = None) -> None:
    try:
        if field is None:
            await redis.incr(key)
        else:
            await redis.hincrby(key, field, 1)
    except RedisError as exc:
        log.warning("stats_unavailable", op=command, error=type(exc).__name__)


async def record_product_view(request: Request, injector_id: int) -> None:
    """Просмотр карточки товара. Вызывается из обработчика после того, как карточка успешно получена."""
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not _is_countable_visitor(headers, getattr(request.state, "session", None)):
        return
    redis: Redis = request.app.state.redis
    await _incr(redis, "product_view", f"{PRODUCT_PREFIX}{today_key()}", str(injector_id))


class StatsMiddleware:
    """Считает просмотры страниц. Чистый ASGI-middleware: не буферизует ответ и не мешает остальным."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return

        status = 0
        content_type = ""

        async def capture(message: Message) -> None:
            nonlocal status, content_type
            if message["type"] == "http.response.start":
                status = int(message["status"])
                for name, value in message.get("headers", []):
                    if name.lower() == b"content-type":
                        content_type = value.decode("latin-1")
            await send(message)

        await self.app(scope, receive, capture)

        path = scope.get("path", "")
        if status != 200 or not content_type.startswith("text/html"):
            return
        if path.startswith(_IGNORED_PREFIXES):
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        session = scope.get("state", {}).get("session")
        if not _is_countable_visitor(headers, session):
            return
        redis: Redis = scope["app"].state.redis
        await _incr(redis, "page_view", f"{VIEWS_PREFIX}{today_key()}")
