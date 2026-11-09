"""Сквозной correlation_id и лог каждого HTTP-запроса.

Реализовано чистым ASGI-middleware, а не BaseHTTPMiddleware: последний запускает приложение
в отдельной задаче и теряет contextvars, на которых держится correlation_id в логах.
"""

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "X-Request-ID"
# Чужой id попадает в логи, поэтому принимаем только безопасные символы (защита от подделки логов).
_VALID_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

log = structlog.get_logger("http")


def current_correlation_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else None


class CorrelationIdMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get(CORRELATION_HEADER)
        correlation_id = (
            incoming if incoming and _VALID_ID.fullmatch(incoming) else uuid.uuid4().hex
        )
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)

        status_code = 500  # если приложение упало до ответа, клиент получит 500

        async def send_with_header(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_with_header)
        finally:
            path = scope["path"]
            # Пробы здоровья вызываются каждые несколько секунд: успешные пишем на уровне debug.
            quiet = path.startswith("/health/") and status_code < 400
            (log.debug if quiet else log.info)(
                "http_request",
                method=scope["method"],
                path=path,
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
