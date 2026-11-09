"""Relay: переносит события из таблицы outbox в RabbitMQ.

Алгоритм одной итерации (в одной транзакции):
  1. SELECT неотправленных строк ... FOR UPDATE SKIP LOCKED
       FOR UPDATE блокирует выбранные строки; SKIP LOCKED заставляет другие экземпляры relay пропускать
       заблокированные, а не ждать. Несколько relay (или воркеров) работают параллельно и не берут одно и то же.
  2. публикация каждой строки с подтверждением брокера (publisher confirms);
  3. пометка sent_at и commit.
Если процесс упал между шагами 2 и 3, строки остались неотправленными, и новый relay отправит их ещё раз (дубль).
Это допустимо: message_id у сообщения тот же, и получатель отсечёт дубль через inbox.
"""

import asyncio
import contextlib
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.messaging.errors import PublishFailed
from app.core.messaging.publisher import Publisher
from app.core.outbox import OutboxMessage

log = structlog.get_logger("relay")


class OutboxRelay:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: Publisher,
        *,
        batch_size: int,
        poll_interval: float,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._batch_size = batch_size
        self._poll_interval = poll_interval

    async def run_once(self) -> int:
        """Отправляет одну пачку. Возвращает число отправленных событий."""
        async with self._session_factory() as session:
            rows = (
                await session.scalars(
                    select(OutboxMessage)
                    .where(OutboxMessage.sent_at.is_(None))
                    .order_by(OutboxMessage.created_at, OutboxMessage.id)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()

            sent = 0
            for row in rows:
                try:
                    await self._publisher.publish_event(
                        row.routing_key,
                        row.payload,
                        message_id=row.id,
                        correlation_id=row.correlation_id,
                    )
                except PublishFailed:
                    # Брокер недоступен или отверг сообщение: остановка пачки. Уже отправленные ниже всё равно
                    # будут помечены, оставшиеся повторятся на следующей итерации.
                    break
                row.sent_at = datetime.now(UTC)
                sent += 1
            await session.commit()
        if sent:
            log.info("outbox_batch_sent", count=sent)
        return sent

    async def run(self, stop: asyncio.Event) -> None:
        """Крутится, пока не поднят флаг stop. Текущая пачка всегда доводится до конца."""
        while not stop.is_set():
            try:
                sent = await self.run_once()
            except Exception:
                # Любая ошибка (БД, сеть) не должна убивать relay: пишем и пробуем снова после паузы.
                log.exception("relay_iteration_failed")
                sent = 0
            if sent < self._batch_size:  # очередь разобрана: ждём новых событий
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), self._poll_interval)
