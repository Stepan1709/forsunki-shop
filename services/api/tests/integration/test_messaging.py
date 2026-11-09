"""Consumer, retry, DLQ и inbox на настоящем RabbitMQ и Postgres."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import aio_pika
import pytest
from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractChannel, AbstractRobustConnection
from fastapi import FastAPI
from sqlalchemy import func, select
from testcontainers.core.container import DockerContainer

from app.core.config import Settings
from app.core.inbox import ProcessedMessage
from app.core.messaging.consumer import Consumer, MessageContext, Subscription
from app.core.messaging.errors import PermanentError, PublishFailed, TemporaryError
from app.core.messaging.publisher import Publisher
from app.core.messaging.topology import (
    EVENTS_EXCHANGE,
    QueueSpec,
    declare_exchanges,
    declare_queue_set,
)
from app.modules.catalog.models import Manufacturer

Handler = Callable[[MessageContext], Awaitable[None]]


# ----------------------------------------------------------------------------------------- помощники
def make_spec(delays: tuple[float, ...] = (0.3, 0.3)) -> QueueSpec:
    """Уникальные очередь и ключ на каждый тест: тесты не видят сообщения друг друга."""
    suffix = uuid.uuid4().hex[:8]
    return QueueSpec(f"test.{suffix}", (f"test.{suffix}.event",), delays)


async def wait_until(predicate: Callable[[], bool], limit: float = 10.0) -> None:
    async with asyncio.timeout(limit):
        while not predicate():  # noqa: ASYNC110  (опрос обычного условия, события нет)
            await asyncio.sleep(0.02)


async def message_count(channel: AbstractChannel, queue_name: str) -> int:
    queue = await channel.declare_queue(
        queue_name, durable=True, arguments={"x-queue-type": "quorum"}, passive=True
    )
    return int(queue.declaration_result.message_count or 0)


async def pop_message(channel: AbstractChannel, queue_name: str) -> Any:
    queue = await channel.get_queue(queue_name, ensure=False)
    message = await queue.get(timeout=5, fail=False)
    assert message is not None, f"в очереди {queue_name} нет сообщений"
    await message.ack()
    return message


class Harness:
    """Запускает Consumer с заданным обработчиком и ведёт учёт вызовов."""

    def __init__(self, app: FastAPI, amqp: AbstractRobustConnection, publisher: Publisher) -> None:
        self._app = app
        self._amqp = amqp
        self._publisher = publisher
        self.calls: list[MessageContext] = []

    @asynccontextmanager
    async def running(
        self,
        spec: QueueSpec,
        handler: Handler,
        *,
        connection: AbstractRobustConnection | None = None,
    ) -> AsyncIterator[Consumer]:
        async def recording(ctx: MessageContext) -> None:
            self.calls.append(ctx)
            await handler(ctx)

        consumer = Consumer(
            Subscription(spec, recording),
            connection or self._amqp,
            self._app.state.resources.session_factory,
            self._publisher,
            default_prefetch=5,
            shutdown_timeout=5,
        )
        await consumer.start()
        try:
            yield consumer
        finally:
            await consumer.stop()


async def send(
    publisher: Publisher,
    spec: QueueSpec,
    data: dict[str, Any] | None = None,
    message_id: uuid.UUID | None = None,
) -> uuid.UUID:
    message_id = message_id or uuid.uuid4()
    await publisher.publish_event(
        spec.routing_keys[0], data or {"n": 1}, message_id=message_id, correlation_id="cid-test"
    )
    return message_id


async def ok(_: MessageContext) -> None:
    return None


async def count_rows(app: FastAPI, model: Any) -> int:
    async with app.state.resources.session_factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ----------------------------------------------------------------------------------------- топология
async def test_topology_is_idempotent_and_uses_quorum_queues(
    channel: AbstractChannel, rabbitmq: DockerContainer
) -> None:
    spec = make_spec()
    events, dlx = await declare_exchanges(channel)

    await declare_queue_set(channel, spec, events, dlx)
    await declare_queue_set(channel, spec, events, dlx)  # повторное объявление не должно падать

    output = rabbitmq.exec(
        ["rabbitmqctl", "list_queues", "name", "type", "--quiet"]
    ).output.decode()
    types = dict(line.split("\t") for line in output.splitlines() if "\t" in line)
    for name in (spec.name, spec.dlq, f"{spec.name}.retry.1", f"{spec.name}.retry.2"):
        assert types[name] == "quorum"


async def test_unroutable_message_is_not_silently_lost(publisher: Publisher) -> None:
    """mandatory=True: сообщение, которому некуда лечь, даёт ошибку, а не пропадает молча."""
    with pytest.raises(PublishFailed):
        await publisher.publish_event(
            "nobody.listens.here", {}, message_id=uuid.uuid4(), correlation_id=None
        )


# ----------------------------------------------------------------------------------------- consumer
async def test_successful_message_is_acked_and_registered_in_inbox(
    app: FastAPI, harness: Harness, publisher: Publisher, channel: AbstractChannel
) -> None:
    spec = make_spec()
    async with harness.running(spec, ok):
        message_id = await send(publisher, spec, {"user_id": "u1"})
        await wait_until(lambda: len(harness.calls) == 1)

    assert harness.calls[0].message_id == message_id
    assert harness.calls[0].data == {"user_id": "u1"}
    assert harness.calls[0].correlation_id == "cid-test"
    assert await message_count(channel, spec.name) == 0
    assert await message_count(channel, spec.dlq) == 0
    assert await count_rows(app, ProcessedMessage) == 1


async def test_temporary_error_is_retried_until_success(
    harness: Harness, publisher: Publisher, channel: AbstractChannel
) -> None:
    spec = make_spec()

    async def flaky(ctx: MessageContext) -> None:
        if ctx.retry_count < 2:
            raise TemporaryError("сервис ещё недоступен")

    async with harness.running(spec, flaky):
        await send(publisher, spec)
        await wait_until(lambda: len(harness.calls) == 3)

    assert [c.retry_count for c in harness.calls] == [0, 1, 2]
    assert len({c.message_id for c in harness.calls}) == 1  # message_id при повторах не меняется
    assert await message_count(channel, spec.dlq) == 0


async def test_retries_are_limited_then_message_goes_to_dlq_with_reason(
    harness: Harness, publisher: Publisher, channel: AbstractChannel
) -> None:
    spec = make_spec(delays=(0.2, 0.2))  # 2 повтора: всего 3 попытки

    async def always_fails(_: MessageContext) -> None:
        raise TemporaryError("БД недоступна")

    async with harness.running(spec, always_fails):
        message_id = await send(publisher, spec, {"k": "v"})
        await wait_until(lambda: len(harness.calls) == 3)
        await asyncio.sleep(0.8)  # убеждаемся, что четвёртой попытки нет

    assert len(harness.calls) == 3
    dead = await pop_message(channel, spec.dlq)
    assert dead.message_id == str(message_id)
    assert json.loads(dead.body)["data"] == {"k": "v"}  # тело не изменено
    assert dead.headers["x-error-type"] == "TemporaryError"
    assert "лимит повторов (2) исчерпан" in dead.headers["x-error"]
    assert "БД недоступна" in dead.headers["x-error"]
    assert dead.headers["x-retry"] == 2
    assert dead.headers["x-original-queue"] == spec.name
    assert "x-failed-at" in dead.headers


@pytest.mark.parametrize(
    ("error", "error_type"),
    [
        (PermanentError("нарушено бизнес-правило"), "PermanentError"),
        (ValueError("баг"), "ValueError"),
    ],
)
async def test_permanent_and_unknown_errors_go_straight_to_dlq(
    harness: Harness,
    publisher: Publisher,
    channel: AbstractChannel,
    error: Exception,
    error_type: str,
) -> None:
    spec = make_spec()

    async def fails(_: MessageContext) -> None:
        raise error

    async with harness.running(spec, fails):
        await send(publisher, spec)
        await wait_until(lambda: len(harness.calls) == 1)
        await asyncio.sleep(0.8)

    assert len(harness.calls) == 1  # повторов нет
    dead = await pop_message(channel, spec.dlq)
    assert dead.headers["x-error-type"] == error_type
    assert "x-retry" not in dead.headers


async def test_malformed_message_goes_to_dlq_without_calling_handler(
    app: FastAPI, harness: Harness, channel: AbstractChannel
) -> None:
    spec = make_spec()
    events = await channel.get_exchange(EVENTS_EXCHANGE, ensure=False)
    async with harness.running(spec, ok):
        await events.publish(
            Message(
                b"<html>not json</html>",
                message_id=str(uuid.uuid4()),
                delivery_mode=DeliveryMode.PERSISTENT,
            ),
            routing_key=spec.routing_keys[0],
            mandatory=True,
        )
        await asyncio.sleep(0.8)

    assert harness.calls == []
    dead = await pop_message(channel, spec.dlq)
    assert dead.headers["x-error-type"] == "PermanentError"


async def test_duplicate_message_is_processed_once(
    app: FastAPI, harness: Harness, publisher: Publisher, channel: AbstractChannel
) -> None:
    spec = make_spec()
    same_id = uuid.uuid4()
    async with harness.running(spec, ok):
        await send(publisher, spec, message_id=same_id)
        await send(publisher, spec, message_id=same_id)  # тот же message_id: дубль
        await send(publisher, spec)  # другое сообщение для контроля
        await wait_until(lambda: len(harness.calls) == 2)
        await asyncio.sleep(0.5)

    assert len(harness.calls) == 2  # дубль отсечён
    assert await message_count(channel, spec.name) == 0  # и подтверждён (ack), а не завис
    assert await count_rows(app, ProcessedMessage) == 2


async def test_business_effect_and_inbox_record_roll_back_together(
    app: FastAPI, harness: Harness, publisher: Publisher
) -> None:
    """Обработчик записал в БД и упал: откатываются и эффект, и запись inbox, поэтому повтор отработает заново."""
    spec = make_spec(delays=(0.2,))

    async def writes_then_fails_once(ctx: MessageContext) -> None:
        ctx.session.add(Manufacturer(name="Cummins", slug="cummins"))
        await ctx.session.flush()
        if ctx.retry_count == 0:
            raise TemporaryError("упал после записи")

    async with harness.running(spec, writes_then_fails_once):
        await send(publisher, spec)
        await wait_until(lambda: len(harness.calls) == 2)
        await asyncio.sleep(0.4)

    # Если бы эффект первой попытки не откатился, вторая упала бы на UNIQUE(name) и сообщение ушло бы в DLQ.
    assert await count_rows(app, Manufacturer) == 1
    assert await count_rows(app, ProcessedMessage) == 1


async def test_each_subscriber_processes_the_same_event(
    harness: Harness, publisher: Publisher, app: FastAPI
) -> None:
    """Fan-out: два подписчика на одно событие. Запись inbox ключом (message_id, consumer), поэтому оба отработают."""
    suffix = uuid.uuid4().hex[:8]
    key = f"test.{suffix}.event"
    spec_a = QueueSpec(f"test.a.{suffix}", (key,), (0.2,))
    spec_b = QueueSpec(f"test.b.{suffix}", (key,), (0.2,))
    seen: list[str] = []

    async def handler_a(_: MessageContext) -> None:
        seen.append("a")

    async def handler_b(_: MessageContext) -> None:
        seen.append("b")

    async with harness.running(spec_a, handler_a), harness.running(spec_b, handler_b):
        await send(publisher, spec_a)
        await wait_until(lambda: len(seen) == 2)

    assert sorted(seen) == ["a", "b"]
    assert await count_rows(app, ProcessedMessage) == 2


async def test_unacked_message_is_redelivered_to_another_consumer_after_crash(
    app: FastAPI,
    infra_settings: Settings,
    amqp: AbstractRobustConnection,
    publisher: Publisher,
) -> None:
    """Воркер взял сообщение и «умер» до ack (обрыв соединения): брокер отдаёт сообщение другому воркеру."""
    spec = make_spec()
    crashing_connection = await aio_pika.connect_robust(infra_settings.rabbitmq_url)
    entered = asyncio.Event()
    never = asyncio.Event()  # не будет установлен: имитация зависшей/упавшей обработки
    processed_by_b: list[uuid.UUID] = []

    async def hangs(_: MessageContext) -> None:
        entered.set()
        await never.wait()

    async def handler_b(ctx: MessageContext) -> None:
        processed_by_b.append(ctx.message_id)

    session_factory = app.state.resources.session_factory
    consumer_a = Consumer(
        Subscription(spec, hangs),
        crashing_connection,
        session_factory,
        publisher,
        default_prefetch=1,
        shutdown_timeout=1,
    )
    await consumer_a.start()
    message_id = await send(publisher, spec)
    await asyncio.wait_for(entered.wait(), 10)  # А взял сообщение и «работает»

    consumer_b = Consumer(
        Subscription(spec, handler_b),
        amqp,
        session_factory,
        publisher,
        default_prefetch=1,
        shutdown_timeout=5,
    )
    await consumer_b.start()
    await crashing_connection.close()  # «убиваем» воркер А: сообщение осталось без ack

    await wait_until(lambda: len(processed_by_b) == 1)
    await consumer_b.stop()

    assert processed_by_b == [message_id]


async def test_stop_waits_for_message_in_progress(
    harness: Harness, publisher: Publisher, channel: AbstractChannel
) -> None:
    """Graceful shutdown: stop() не возвращается, пока текущее сообщение не обработано и не подтверждено."""
    spec = make_spec()
    started = asyncio.Event()
    finished: list[bool] = []

    async def slow(_: MessageContext) -> None:
        started.set()
        await asyncio.sleep(1.0)
        finished.append(True)

    async with harness.running(spec, slow) as consumer:
        await send(publisher, spec)
        await asyncio.wait_for(started.wait(), 10)
        await consumer.stop()
        assert finished == [True]  # к моменту возврата из stop() сообщение обработано
        assert await message_count(channel, spec.name) == 0  # и подтверждено

    # повторный stop (из контекста harness) безопасен
