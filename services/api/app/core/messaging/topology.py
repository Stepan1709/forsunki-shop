"""Топология RabbitMQ: обмены и очереди. Объявляется идемпотентно при старте каждого процесса.

Схема для рабочей очереди Q = "notifications.user_registered":

  forsunki.events (topic) --routing key "user.registered"--> Q
  Q ---ошибка TemporaryError---> forsunki.dlx --"Q.retry.k"--> Q.retry.k (TTL) --истёк--> forsunki.dlx --"Q"--> Q
  Q ---PermanentError / лимит повторов---> forsunki.dlx --"Q.dlq"--> Q.dlq (ждёт человека)

Повтор не использует плагинов: задержку даёт TTL очереди, а возврат в рабочую очередь делает dead-lettering.
Ярусы (`Q.retry.1`, `Q.retry.2`, ...) нужны потому, что TTL очереди фиксирован: сообщение с долгой паузой в общей
очереди задержало бы стоящее за ним сообщение с короткой.

Аргументы очереди после создания менять нельзя (RabbitMQ вернёт PRECONDITION_FAILED). Поменяли задержки или лимит:
удалите старые очереди (в dev это безопасно) или объявите новые под другим именем.
"""

from dataclasses import dataclass
from typing import Any

from aio_pika import ExchangeType
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractQueue

EVENTS_EXCHANGE = "forsunki.events"
DLX_EXCHANGE = "forsunki.dlx"


@dataclass(frozen=True)
class QueueSpec:
    name: str
    routing_keys: tuple[str, ...]
    # Пауза перед каждым следующим повтором, секунды. Длина = максимум повторов.
    retry_delays_seconds: tuple[float, ...] = (5.0, 30.0, 120.0)
    # Защита от «ядовитого» сообщения при падениях consumer'а: после стольких доставок без ack
    # quorum-очередь сама отправит его в DLQ.
    delivery_limit: int = 10

    @property
    def dlq(self) -> str:
        return f"{self.name}.dlq"

    @property
    def max_retries(self) -> int:
        return len(self.retry_delays_seconds)

    def retry_queue(self, attempt: int) -> str:
        """Имя яруса для попытки с номером `attempt` (с единицы)."""
        return f"{self.name}.retry.{min(attempt, self.max_retries)}"


_QUORUM: dict[str, Any] = {"x-queue-type": "quorum"}


async def declare_exchanges(channel: AbstractChannel) -> tuple[AbstractExchange, AbstractExchange]:
    events = await channel.declare_exchange(EVENTS_EXCHANGE, ExchangeType.TOPIC, durable=True)
    dlx = await channel.declare_exchange(DLX_EXCHANGE, ExchangeType.DIRECT, durable=True)
    return events, dlx


async def declare_queue_set(
    channel: AbstractChannel,
    spec: QueueSpec,
    events: AbstractExchange,
    dlx: AbstractExchange,
) -> AbstractQueue:
    """Объявляет рабочую очередь, её DLQ и ярусы повторов. Возвращает рабочую очередь."""
    # DLQ: сюда попадает всё, что нельзя обработать. Хранится, пока человек не разберёт.
    dlq = await channel.declare_queue(spec.dlq, durable=True, arguments=_QUORUM)
    await dlq.bind(dlx, routing_key=spec.dlq)

    # Ярусы повторов: сообщение лежит TTL миллисекунд, затем dead-letter возвращает его в рабочую очередь.
    for attempt, delay in enumerate(spec.retry_delays_seconds, start=1):
        retry = await channel.declare_queue(
            f"{spec.name}.retry.{attempt}",
            durable=True,
            arguments={
                **_QUORUM,
                "x-message-ttl": int(delay * 1000),
                "x-dead-letter-exchange": DLX_EXCHANGE,
                "x-dead-letter-routing-key": spec.name,
            },
        )
        await retry.bind(dlx, routing_key=f"{spec.name}.retry.{attempt}")

    work = await channel.declare_queue(
        spec.name,
        durable=True,
        arguments={
            **_QUORUM,
            "x-dead-letter-exchange": DLX_EXCHANGE,
            "x-dead-letter-routing-key": spec.dlq,
            "x-delivery-limit": spec.delivery_limit,
        },
    )
    for key in spec.routing_keys:
        await work.bind(events, routing_key=key)
    # Возврат из яруса повтора приходит через dlx с ключом = имя очереди.
    await work.bind(dlx, routing_key=spec.name)
    return work
