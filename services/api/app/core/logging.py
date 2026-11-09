"""Структурированное логирование: JSON в stdout (12-factor).

Контейнер не пишет логи в файлы: их собирает Docker (json-file с ротацией), а дальше Fluent Bit.
Через structlog идут и логи библиотек (uvicorn, sqlalchemy, aio-pika), поэтому формат единый.
"""

import logging
import sys

import structlog
from structlog.processors import JSONRenderer, TimeStamper, format_exc_info
from structlog.types import EventDict, Processor

from app.core.config import Settings

# Значения этих ключей никогда не попадают в лог, даже если их случайно передали.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "token",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "secret",
        "api_key",
    }
)


def _redact(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
    for key in event_dict:
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = "***"
    return event_dict


def configure_logging(settings: Settings) -> None:
    def add_static_fields(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = settings.service_name
        event_dict["env"] = settings.env
        # Поле обязательно в каждой записи; вне запроса (старт, воркер) значения нет.
        event_dict.setdefault("correlation_id", None)
        return event_dict

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,  # подмешивает correlation_id текущего запроса
        structlog.stdlib.add_log_level,
        TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_static_fields,
        _redact,
    ]
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
    )

    # foreign_pre_chain применяется к записям «чужих» библиотек (обычный logging).
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

    # uvicorn ставит свои обработчики; убираем их, чтобы всё шло через общий JSON-формат.
    for name in ("uvicorn", "uvicorn.error"):
        lib_logger = logging.getLogger(name)
        lib_logger.handlers = []
        lib_logger.propagate = True

    # Запросы логирует CorrelationIdMiddleware (с correlation_id и длительностью), поэтому
    # access-лог uvicorn отключён: иначе каждый запрос попадал бы в логи дважды.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False
