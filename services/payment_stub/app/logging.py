"""JSON-логи в stdout, как у остальных сервисов (поля timestamp, level, service, env, event)."""

import logging
import sys

import structlog
from structlog.processors import JSONRenderer, TimeStamper, format_exc_info
from structlog.types import EventDict, Processor

from app.config import Settings
from app.tracing import current_trace_ids


def configure_logging(settings: Settings) -> None:
    def add_static_fields(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
        event_dict["service"] = settings.service_name
        event_dict["env"] = settings.env
        event_dict.setdefault("correlation_id", None)
        return event_dict

    def add_trace_ids(_logger: object, _method: str, event_dict: EventDict) -> EventDict:
        ids = current_trace_ids()
        if ids:
            event_dict["trace_id"], event_dict["span_id"] = ids
        return event_dict

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_static_fields,
        add_trace_ids,
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
        lib_logger = logging.getLogger(name)
        lib_logger.handlers = []
        lib_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers = []
    access.propagate = False
