"""Consumer: надёжная обработка сообщений с ручным ack, inbox, повторами и DLQ.

Жизненный цикл одного сообщения:

  пришло -> разобрать -> [транзакция: inbox.try_register -> handler -> commit] -> ack
                │                       │
                │                       ├─ дубль (inbox уже содержит id) ......... ack, обработчик не вызываем
                │                       ├─ TemporaryError / сбой сети или БД ..... в ярус повтора, затем ack
                │                       └─ PermanentError / неизвестная ошибка ... в DLQ с причиной, затем ack
                └─ битое сообщение ........ PermanentError -> в DLQ

ack ставится ПОСЛЕ успешной обработки. Если процесс упал до ack, брокер вернёт сообщение в очередь и отдаст другому
consumer'у (at-least-once); повторная обработка безопасна благодаря inbox.
Порядок «опубликовать в повтор, потом ack» тоже не атомарен: сбой между ними даёт дубль, а не потерю. Это
осознанный выбор в пользу «лучше дважды, чем ни разу».
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from aio_pika import DeliveryMode, Message
from aio_pika.abc import (
    AbstractChannel,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)
from opentelemetry.trace import SpanKind
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import inbox
from app.core.messaging.errors import PermanentError, PublishFailed, TemporaryError
from app.core.messaging.message import RETRY_HEADER, ParsedMessage, get_retry_count, parse_message
from app.core.messaging.publisher import Publisher
from app.core.messaging.topology import QueueSpec, declare_exchanges, declare_queue_set
from app.core.metrics import CONSUMER_DURATION, CONSUMER_MESSAGES
from app.core.tracing import span

log = structlog.get_logger("consumer")

# Сбои инфраструктуры, которые проходят сами: повторяем, а не отправляем сразу в DLQ.
TRANSIENT_ERRORS = (TemporaryError, OperationalError, InterfaceError, ConnectionError, TimeoutError)

_MAX_REASON_LENGTH = 500


@dataclass(frozen=True)
class MessageContext:
    """Всё, что нужно обработчику. session уже в транзакции вместе с записью inbox."""

    session: AsyncSession
    message_id: uuid.UUID
    event_type: str
    correlation_id: str | None
    data: dict[str, Any]
    retry_count: int


Handler = Callable[[MessageContext], Awaitable[None]]


@dataclass(frozen=True)
class Subscription:
    spec: QueueSpec
    handler: Handler
    prefetch: int | None = None  # None: взять из настроек


def _clone(message: AbstractIncomingMessage, headers: dict[str, Any]) -> Message:
    """Копия сообщения для повтора/DLQ. message_id, тело и correlation_id сохраняются без изменений."""
    return Message(
        body=message.body,
        headers=headers,
        content_type=message.content_type,
        content_encoding=message.content_encoding,
        delivery_mode=DeliveryMode.PERSISTENT,
        message_id=message.message_id,
        correlation_id=message.correlation_id,
        type=message.type,
        timestamp=message.timestamp,
    )


def _base_headers(message: AbstractIncomingMessage) -> dict[str, Any]:
    # Служебные заголовки истории dead-lettering брокер добавляет сам; в копию их не переносим.
    return {
        key: value
        for key, value in (message.headers or {}).items()
        if not key.startswith(("x-death", "x-first-death", "x-last-death"))
    }


class Consumer:
    def __init__(
        self,
        subscription: Subscription,
        connection: AbstractRobustConnection,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: Publisher,
        *,
        default_prefetch: int,
        shutdown_timeout: float,
    ) -> None:
        self._sub = subscription
        self._connection = connection
        self._session_factory = session_factory
        self._publisher = publisher
        self._prefetch = subscription.prefetch or default_prefetch
        self._shutdown_timeout = shutdown_timeout

        self._channel: AbstractChannel | None = None
        self._queue: AbstractQueue | None = None
        self._consumer_tag: str | None = None
        self._inflight = 0
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def name(self) -> str:
        return self._sub.spec.name

    async def start(self) -> None:
        # Отдельный канал на consumer: ошибка в одном не закрывает остальные.
        self._channel = await self._connection.channel()
        # prefetch: сколько сообщений брокер выдаёт без подтверждения. Ограничивает параллелизм и не даёт одному
        # consumer'у «набрать» всю очередь, пока другие простаивают.
        await self._channel.set_qos(prefetch_count=self._prefetch)
        events, dlx = await declare_exchanges(self._channel)
        self._queue = await declare_queue_set(self._channel, self._sub.spec, events, dlx)
        self._consumer_tag = await self._queue.consume(
            self._on_message
        )  # ручной ack (no_ack=False)
        log.info("consumer_started", queue=self.name, prefetch=self._prefetch)

    async def stop(self) -> None:
        """Остановка: перестать брать новые сообщения, дождаться текущих, закрыть канал."""
        if self._queue is not None and self._consumer_tag is not None:
            await self._queue.cancel(self._consumer_tag)  # новые сообщения больше не приходят
            self._consumer_tag = None
        try:
            await asyncio.wait_for(self._idle.wait(), self._shutdown_timeout)
        except TimeoutError:
            # Не дождались: сообщения без ack вернутся в очередь при закрытии канала (at-least-once).
            log.warning("consumer_stop_timeout", queue=self.name, inflight=self._inflight)
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        log.info("consumer_stopped", queue=self.name)

    # ------------------------------------------------------------------------------------------------
    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        self._inflight += 1
        self._idle.clear()
        try:
            await self._handle(message)
        finally:
            self._inflight -= 1
            if self._inflight == 0:
                self._idle.set()

    async def _handle(self, message: AbstractIncomingMessage) -> None:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=message.correlation_id, message_id=message.message_id, queue=self.name
        )
        started = time.perf_counter()
        # Спан обработки продолжает трассу публикации (traceparent в заголовках). Повторы и DLQ копируют заголовки,
        # поэтому все попытки обработки одного сообщения остаются в одной трассе.
        with span(
            f"consume {self.name}",
            kind=SpanKind.CONSUMER,
            parent_headers=message.headers,
            attributes={
                "messaging.system": "rabbitmq",
                "messaging.destination.name": self.name,
                "messaging.message.id": message.message_id or "",
            },
        ):
            try:
                parsed = parse_message(message)
                outcome = await self._process(parsed)
                await message.ack()
                CONSUMER_MESSAGES.labels(self.name, outcome).inc()
            except TRANSIENT_ERRORS as exc:
                await self._retry_or_dead_letter(message, exc)
            except PermanentError as exc:
                await self._dead_letter(message, exc)
            except Exception as exc:
                # Неизвестное исключение считаем постоянным: скорее всего это баг, и повторы его не вылечат.
                log.exception("unexpected_error")
                await self._dead_letter(message, exc)
            finally:
                CONSUMER_DURATION.labels(self.name).observe(time.perf_counter() - started)

    async def _process(self, parsed: ParsedMessage) -> str:
        """Возвращает исход для метрик: processed или duplicate."""
        async with self._session_factory() as session:
            # Запись inbox и бизнес-эффект в ОДНОЙ транзакции: откат уберёт обе.
            if not await inbox.try_register(session, parsed.message_id, self.name):
                log.info("duplicate_skipped")
                return "duplicate"
            await self._sub.handler(
                MessageContext(
                    session=session,
                    message_id=parsed.message_id,
                    event_type=parsed.event_type,
                    correlation_id=parsed.correlation_id,
                    data=parsed.data,
                    retry_count=parsed.retry_count,
                )
            )
            await session.commit()
        return "processed"

    async def _retry_or_dead_letter(self, message: AbstractIncomingMessage, exc: Exception) -> None:
        spec = self._sub.spec
        attempt = get_retry_count(message) + 1
        if attempt > spec.max_retries:
            await self._dead_letter(
                message, exc, reason_prefix=f"лимит повторов ({spec.max_retries}) исчерпан: "
            )
            return
        headers = {**_base_headers(message), RETRY_HEADER: attempt}
        try:
            await self._publisher.publish_to_dlx(
                spec.retry_queue(attempt), _clone(message, headers)
            )
        except PublishFailed:
            # Не смогли положить в повтор: возвращаем сообщение в очередь, чтобы не потерять.
            await message.nack(requeue=True)
            return
        log.warning("message_scheduled_for_retry", attempt=attempt, error=type(exc).__name__)
        await message.ack()
        CONSUMER_MESSAGES.labels(self.name, "retried").inc()

    async def _dead_letter(
        self, message: AbstractIncomingMessage, exc: Exception, *, reason_prefix: str = ""
    ) -> None:
        headers = {
            **_base_headers(message),
            # Причина и контекст сохраняются в самом сообщении: разбирающему не нужно искать их в логах.
            "x-error": (reason_prefix + str(exc))[:_MAX_REASON_LENGTH],
            "x-error-type": type(exc).__name__,
            "x-failed-at": datetime.now(UTC).isoformat(),
            "x-original-queue": self.name,
        }
        try:
            await self._publisher.publish_to_dlx(self._sub.spec.dlq, _clone(message, headers))
        except PublishFailed:
            await message.nack(requeue=True)
            return
        log.error("message_dead_lettered", error=type(exc).__name__)
        await message.ack()
        CONSUMER_MESSAGES.labels(self.name, "dead_lettered").inc()
