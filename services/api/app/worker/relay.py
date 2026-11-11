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
from datetime import UTC, datetime, timedelta

import structlog
from opentelemetry.trace import SpanKind
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.messaging.errors import PublishFailed, Unroutable
from app.core.messaging.publisher import Publisher
from app.core.outbox import OutboxMessage
from app.core.tracing import inject_headers, span

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
                    .where(
                        OutboxMessage.sent_at.is_(None),
                        OutboxMessage.next_attempt_at <= func.now(),
                    )
                    .order_by(OutboxMessage.created_at, OutboxMessage.id)
                    .limit(self._batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()

            sent = 0
            for row in rows:
                try:
                    # Спан публикации продолжает трассу запроса, создавшего событие (traceparent из строки outbox), а его
                    # собственный контекст уходит в заголовок сообщения: consumer станет потомком этого спана.
                    with span(
                        f"publish {row.routing_key}",
                        kind=SpanKind.PRODUCER,
                        parent_traceparent=row.traceparent,
                        attributes={
                            "messaging.system": "rabbitmq",
                            "messaging.destination.name": row.routing_key,
                        },
                    ):
                        await self._publisher.publish_event(
                            row.routing_key,
                            row.payload,
                            message_id=row.id,
                            correlation_id=row.correlation_id,
                            headers=inject_headers({}),
                        )
                except Unroutable as exc:
                    # У события нет получателя. Это проблема конкретного сообщения, а не брокера: откладываем его
                    # с растущей паузой и идём дальше, чтобы оно не блокировало остальные события.
                    self._defer(row, exc)
                    continue
                except PublishFailed:
                    # Брокер недоступен: остановка пачки. Уже отправленные всё равно будут помечены,
                    # остальные повторятся на следующей итерации.
                    break
                row.sent_at = datetime.now(UTC)
                sent += 1
            await session.commit()
        if sent:
            log.info("outbox_batch_sent", count=sent)
        return sent

    @staticmethod
    def _defer(row: OutboxMessage, exc: Exception) -> None:
        row.attempts += 1
        row.last_error = str(exc)[:500]
        # Экспоненциальная пауза: 10 с, 20 с, 40 с ... не дольше часа.
        delay = min(10 * 2 ** (row.attempts - 1), 3600)
        row.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        log.warning("outbox_event_unroutable", routing_key=row.routing_key, attempts=row.attempts)

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
