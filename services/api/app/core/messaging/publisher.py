"""Публикация сообщений с подтверждением брокера (publisher confirms) и флагом mandatory."""

import uuid
from typing import Any

import structlog
from aio_pika import Message
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection
from aio_pika.exceptions import AMQPError, DeliveryError, PublishError

from app.core.messaging.errors import PublishFailed, Unroutable
from app.core.messaging.message import build_message
from app.core.messaging.topology import declare_exchanges

log = structlog.get_logger("publisher")


class Publisher:
    """Публикует в обмены forsunki.events и forsunki.dlx.

    publisher confirms: `publish` ждёт, пока брокер подтвердит, что принял и сохранил сообщение. Без этого
    сообщение могло бы потеряться между клиентом и диском брокера, а мы считали бы его отправленным.
    mandatory=True: если сообщение некуда доставить (нет подходящей очереди), брокер вернёт его, и мы получим
    ошибку, а не тихую потерю.
    """

    def __init__(
        self, channel: AbstractChannel, events: AbstractExchange, dlx: AbstractExchange
    ) -> None:
        self._channel = channel
        self._events = events
        self._dlx = dlx

    @classmethod
    async def open(cls, connection: AbstractRobustConnection) -> "Publisher":
        """Открывает отдельный канал издателя.

        publisher_confirms: ждём подтверждения брокера. on_return_raises: если брокер вернул сообщение (некуда
        доставить), `publish` бросает исключение. Без этого флага aio-pika лишь вызывает callback, и mandatory=True
        ничего бы не защищал: сообщение пропало бы молча.
        """
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        events, dlx = await declare_exchanges(channel)
        return cls(channel, events, dlx)

    async def close(self) -> None:
        await self._channel.close()

    async def publish_event(
        self,
        routing_key: str,
        data: dict[str, Any],
        *,
        message_id: uuid.UUID,
        correlation_id: str | None,
        headers: dict[str, Any] | None = None,
    ) -> None:
        message = build_message(
            routing_key,
            data,
            message_id=message_id,
            correlation_id=correlation_id,
            headers=headers,
        )
        await self._publish(self._events, routing_key, message)

    async def publish_to_dlx(self, routing_key: str, message: Message) -> None:
        """Отправка в повтор или DLQ: сообщение уже собрано (сохраняет message_id и заголовки)."""
        await self._publish(self._dlx, routing_key, message)

    async def _publish(
        self, exchange: AbstractExchange, routing_key: str, message: Message
    ) -> None:
        try:
            await exchange.publish(message, routing_key=routing_key, mandatory=True)
        except PublishError as exc:
            log.error("publish_unroutable", routing_key=routing_key)
            raise Unroutable(f"у события {routing_key} нет получателя") from exc
        except (DeliveryError, AMQPError) as exc:
            log.error("publish_failed", routing_key=routing_key, error=type(exc).__name__)
            raise PublishFailed(f"не удалось опубликовать {routing_key}: {exc}") from exc
