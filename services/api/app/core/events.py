"""Публикация доменных событий.

Бизнес-код знает только интерфейс `EventSink`. Реальная реализация: `OutboxEventSink` (app/core/outbox.py) пишет
событие в таблицу outbox в той же транзакции, что и бизнес-данные (поэтому `publish` вызывается ДО commit), а
relay воркера отправляет его в RabbitMQ.
"""

from typing import Any, Protocol

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.core.outbox import OutboxEventSink


class EventSink(Protocol):
    async def publish(self, routing_key: str, payload: dict[str, Any]) -> None: ...


def get_event_sink(session: AsyncSession = Depends(get_session)) -> EventSink:
    """Зависимость FastAPI. Использует ТУ ЖЕ сессию, что и сервис (FastAPI создаёт зависимость один раз
    на запрос), поэтому событие и бизнес-данные попадают в одну транзакцию."""
    return OutboxEventSink(session)
