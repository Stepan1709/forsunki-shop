"""Страховочный обход: закрывает просроченные заказы, даже если сообщение об истечении потерялось.

Основной путь истечения идёт через очередь с задержкой, но у любого пути есть сценарии отказа (очередь удалили,
сообщение ушло в DLQ, воркер долго не работал). Обход раз в минуту выбирает неоплаченные заказы со сроком оплаты в
прошлом (по частичному индексу) и закрывает их тем же методом, что и обработчик сообщения: повтор безопасен.
"""

import asyncio
import contextlib

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.outbox import OutboxEventSink
from app.modules.orders.service import OrderService

log = structlog.get_logger("sweeper")


class ExpirySweeper:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        *,
        batch_size: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._batch_size = batch_size

    async def run_once(self) -> int:
        """Закрывает пачку просроченных заказов. Возвращает, сколько заказов закрыто."""
        async with self._session_factory() as session:
            service = OrderService(session, self._settings, OutboxEventSink(session))
            ids = await service.expired_pending_ids(
                self._settings.expiry_sweep_grace_seconds, self._batch_size
            )
            closed = 0
            for order_id in ids:
                if await service.expire_if_pending(order_id):
                    closed += 1
            await session.commit()
        if closed:
            log.warning("sweeper_expired_orders", count=closed)
        return closed

    async def run(self, stop: asyncio.Event) -> None:
        interval = self._settings.expiry_sweep_interval_seconds
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("sweeper_iteration_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
