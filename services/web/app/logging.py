"""JSON-логи в stdout и сквозной correlation_id (тот же формат, что у api, чтобы логи искались одним запросом)."""

import logging
import re
import sys
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.processors import JSONRenderer, TimeStamper, format_exc_info
from structlog.types import EventDict, Processor

from app.config import Settings
from app.tracing import current_trace_ids

CORRELATION_HEADER = "X-Request-ID"
_VALID_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_SENSITIVE = frozenset(
    {"password", "token", "access_token", "refresh_token", "authorization", "cookie", "csrf_token"}
)

log = structlog.get_logger("http")


def current_correlation_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return value if isinstance(value, str) else None


def configure_logging(settings: Settings) -> None:
    def add_static(_l: object, _m: str, event: EventDict) -> EventDict:
        event["service"] = settings.service_name
        event["env"] = settings.env
        event.setdefault("correlation_id", None)
        return event

    def redact(_l: object, _m: str, event: EventDict) -> EventDict:
        for key in event:
            if key.lower() in _SENSITIVE:
                event[key] = "***"
        return event

    def add_trace_ids(_l: object, _m: str, event: EventDict) -> EventDict:
        ids = current_trace_ids()
        if ids:
            event["trace_id"], event["span_id"] = ids
        return event

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_static,
        add_trace_ids,
        redact,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            format_exc_info,
            JSONRenderer(ensure_ascii=False),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = False
    # Клиент httpx пишет в лог каждый запрос к api вместе с адресом: нам достаточно собственных записей.
    logging.getLogger("httpx").setLevel(logging.WARNING)


class CorrelationIdMiddleware:
    """Принимает X-Request-ID (nginx) или создаёт его, кладёт в логи и в ответ; дальше он уходит в api."""

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
        status_code = 500

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
            quiet = (
                path.startswith("/health/") or path.startswith("/static/")
            ) and status_code < 400
            (log.debug if quiet else log.info)(
                "http_request",
                method=scope["method"],
                path=path,
                status=status_code,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
