"""Периодическое обновление метрик, которые берутся из БД: заказы по статусам и состояние outbox.

Такие значения нельзя посчитать «по ходу дела» в обработчиках (их меняют разные процессы), поэтому воркер раз в несколько секунд
спрашивает БД и выставляет gauge. Prometheus забирает текущее значение при очередном скрейпе. Если воркеров несколько, у всех
одинаковые значения; в запросах Grafana используется `max`, а не `sum`.
"""

import asyncio
import contextlib
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.metrics import ORDERS_BY_STATUS, OUTBOX_OVERDUE, OUTBOX_PENDING, OUTBOX_WAITING
from app.core.outbox import OutboxMessage
from app.modules.orders.models import Order
from app.modules.orders.state import OrderStatus

log = structlog.get_logger("stats")


class StatsCollector:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], interval_seconds: float
    ) -> None:
        self._session_factory = session_factory
        self._interval = interval_seconds

    async def run_once(self) -> None:
        async with self._session_factory() as session:
            counts = dict(
                (
                    await session.execute(select(Order.status, func.count()).group_by(Order.status))
                ).all()
            )
            now = datetime.now(UTC)
            unsent = OutboxMessage.sent_at.is_(None)
            pending, waiting, due_since = (
                await session.execute(
                    select(
                        func.count(),
                        func.count().filter(OutboxMessage.attempts > 0),
                        func.min(OutboxMessage.next_attempt_at).filter(
                            OutboxMessage.next_attempt_at <= now
                        ),
                    ).where(unsent)
                )
            ).one()
        # Статусы без заказов выставляются в 0: иначе на графике ряд исчезал бы вместо нулевого значения.
        for status in OrderStatus:
            ORDERS_BY_STATUS.labels(status.value).set(counts.get(status, 0))
        OUTBOX_PENDING.set(pending)
        OUTBOX_WAITING.set(waiting)
        OUTBOX_OVERDUE.set(max(0.0, (now - due_since).total_seconds()) if due_since else 0)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("stats_iteration_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), self._interval)
