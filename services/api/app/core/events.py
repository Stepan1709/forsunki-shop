"""Публикация доменных событий.

Бизнес-код знает только интерфейс `EventSink`. Сейчас реализация лишь пишет событие в лог.
На этапе 4 её заменит запись в таблицу outbox в той же транзакции, что и бизнес-данные
(поэтому `publish` вызывается ДО commit), а relay отправит событие в RabbitMQ.
"""

from typing import Any, Protocol

import structlog

log = structlog.get_logger("events")


class EventSink(Protocol):
    async def publish(self, routing_key: str, payload: dict[str, Any]) -> None: ...


class LoggingEventSink:
    async def publish(self, routing_key: str, payload: dict[str, Any]) -> None:
        # Полезную нагрузку не пишем: в ней бывают персональные данные (например, email).
        log.info("event_staged", routing_key=routing_key, entity_id=payload.get("user_id"))


def get_event_sink() -> EventSink:
    return LoggingEventSink()
