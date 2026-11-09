"""Outbox, relay и сквозной путь «регистрация -> событие -> воркер» на настоящих RabbitMQ и Postgres."""

import asyncio
import uuid
from typing import Any

import httpx
from aio_pika.abc import AbstractChannel
from fastapi import FastAPI
from sqlalchemy import func, select, text

from app.core.config import Settings
from app.core.inbox import ProcessedMessage
from app.core.messaging.consumer import MessageContext, Subscription
from app.core.messaging.errors import PublishFailed
from app.core.messaging.publisher import Publisher
from app.core.messaging.topology import QueueSpec, declare_exchanges, declare_queue_set
from app.core.outbox import OutboxEventSink, OutboxMessage
from app.modules.catalog.models import Manufacturer
from app.worker.relay import OutboxRelay
from app.worker.runner import Worker
from tests.integration.test_messaging import make_spec, message_count, wait_until


async def bind_queue(channel: AbstractChannel, spec: QueueSpec) -> None:
    events, dlx = await declare_exchanges(channel)
    await declare_queue_set(channel, spec, events, dlx)


async def add_events(app: FastAPI, routing_key: str, count: int) -> list[uuid.UUID]:
    async with app.state.resources.session_factory() as session:
        sink = OutboxEventSink(session)
        for i in range(count):
            await sink.publish(routing_key, {"n": i})
        await session.commit()
        ids = list((await session.scalars(select(OutboxMessage.id))).all())
    return ids


def make_relay(app: FastAPI, publisher: Any, batch: int = 50) -> OutboxRelay:
    return OutboxRelay(
        app.state.resources.session_factory, publisher, batch_size=batch, poll_interval=0.05
    )


async def drain(channel: AbstractChannel, queue_name: str, expected: int) -> list[Any]:
    queue = await channel.get_queue(queue_name, ensure=False)
    messages: list[Any] = []
    async with asyncio.timeout(10):
        while len(messages) < expected:
            message = await queue.get(timeout=2, fail=False)
            if message is not None:
                await message.ack()
                messages.append(message)
    return messages


# ----------------------------------------------------------------------------------------- outbox
async def test_event_is_saved_atomically_with_business_data(app: FastAPI) -> None:
    factory = app.state.resources.session_factory

    # Откат: ни бизнес-данных, ни события.
    async with factory() as session:
        session.add(Manufacturer(name="Откат", slug="rollback"))
        await OutboxEventSink(session).publish("test.event", {"a": 1})
        await session.rollback()
    # Фиксация: и данные, и событие.
    async with factory() as session:
        session.add(Manufacturer(name="Фиксация", slug="commit"))
        await OutboxEventSink(session).publish("test.event", {"a": 2})
        await session.commit()

    async with factory() as session:
        names = list((await session.scalars(select(Manufacturer.name))).all())
        payloads = list((await session.scalars(select(OutboxMessage.payload))).all())
    assert names == ["Фиксация"]
    assert payloads == [{"a": 2}]


async def test_registration_writes_event_to_outbox_in_same_transaction(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.post(
        "/auth/register",
        json={"email": "new@example.com", "password": "long-enough-pass"},
        headers={"X-Request-ID": "req-abc-123"},
    )

    assert response.status_code == 201
    async with app.state.resources.session_factory() as session:
        row = (await session.scalars(select(OutboxMessage))).one()
    assert row.routing_key == "user.registered"
    assert row.payload == {"user_id": response.json()["id"], "email": "new@example.com"}
    assert row.correlation_id == "req-abc-123"  # связь HTTP-запроса и события
    assert row.sent_at is None


async def test_duplicate_registration_does_not_leave_event_behind(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    body = {"email": "dup@example.com", "password": "long-enough-pass"}
    await client.post("/auth/register", json=body)
    await client.post("/auth/register", json=body)  # 409: транзакция откатилась

    async with app.state.resources.session_factory() as session:
        assert (await session.scalar(select(func.count()).select_from(OutboxMessage))) == 1


# ----------------------------------------------------------------------------------------- relay
async def test_relay_publishes_events_and_marks_them_sent(
    app: FastAPI, channel: AbstractChannel, publisher: Publisher
) -> None:
    spec = make_spec()
    await bind_queue(channel, spec)
    ids = await add_events(app, spec.routing_keys[0], 3)
    relay = make_relay(app, publisher)

    assert await relay.run_once() == 3
    assert await relay.run_once() == 0  # отправленное повторно не берётся

    messages = await drain(channel, spec.name, 3)
    assert {m.message_id for m in messages} == {
        str(i) for i in ids
    }  # message_id = id строки outbox
    async with app.state.resources.session_factory() as session:
        assert (
            await session.scalar(select(func.count()).where(OutboxMessage.sent_at.is_(None)))
        ) == 0


async def test_two_relays_do_not_publish_the_same_event_twice(
    app: FastAPI, channel: AbstractChannel, publisher: Publisher
) -> None:
    """FOR UPDATE SKIP LOCKED: параллельные relay делят работу, а не дублируют её."""
    spec = make_spec()
    await bind_queue(channel, spec)
    await add_events(app, spec.routing_keys[0], 60)

    async def drain_with(relay: OutboxRelay) -> int:
        total = 0
        while sent := await relay.run_once():
            total += sent
        return total

    sent_a, sent_b = await asyncio.gather(
        drain_with(make_relay(app, publisher, batch=10)),
        drain_with(make_relay(app, publisher, batch=10)),
    )

    assert sent_a + sent_b == 60
    messages = await drain(channel, spec.name, 60)
    assert len({m.message_id for m in messages}) == 60  # ни одного дубля
    assert await message_count(channel, spec.name) == 0


class FlakyPublisher:
    """Публикует первые N событий, затем «брокер недоступен»."""

    def __init__(self, real: Publisher, fail_after: int) -> None:
        self._real = real
        self._left = fail_after

    async def publish_event(self, *args: Any, **kwargs: Any) -> None:
        if self._left <= 0:
            raise PublishFailed("брокер недоступен")
        self._left -= 1
        await self._real.publish_event(*args, **kwargs)


async def test_relay_survives_broker_failure_and_restart_without_losing_events(
    app: FastAPI, channel: AbstractChannel, publisher: Publisher
) -> None:
    spec = make_spec()
    await bind_queue(channel, spec)
    await add_events(app, spec.routing_keys[0], 5)

    first = make_relay(app, FlakyPublisher(publisher, fail_after=2))
    assert await first.run_once() == 2  # два ушли, на третьем «упал брокер»

    async with app.state.resources.session_factory() as session:
        unsent = await session.scalar(select(func.count()).where(OutboxMessage.sent_at.is_(None)))
    assert unsent == 3  # остальные остались в БД и не потеряны

    second = make_relay(app, publisher)  # «перезапуск»: новый экземпляр
    assert await second.run_once() == 3

    messages = await drain(channel, spec.name, 5)
    assert len({m.message_id for m in messages}) == 5


# ----------------------------------------------------------------------------------------- воркер целиком
async def test_registered_user_event_reaches_worker_handler_with_same_correlation_id(
    app: FastAPI, client: httpx.AsyncClient, infra_settings: Settings
) -> None:
    """Сквозной путь: HTTP-регистрация -> outbox -> relay -> RabbitMQ -> consumer воркера -> обработчик."""
    suffix = uuid.uuid4().hex[:8]
    spec = QueueSpec(f"test.worker.{suffix}", ("user.registered",), (0.2,))
    received: list[MessageContext] = []

    async def handler(ctx: MessageContext) -> None:
        received.append(ctx)

    settings = infra_settings.model_copy(
        update={"relay_poll_interval_seconds": 0.05, "worker_heartbeat_path": None}
    )
    worker = Worker(settings, [Subscription(spec, handler)])
    await worker.start()
    try:
        response = await client.post(
            "/auth/register",
            json={"email": "e2e@example.com", "password": "long-enough-pass"},
            headers={"X-Request-ID": "e2e-correlation-1"},
        )
        await wait_until(lambda: len(received) == 1, limit=15)
    finally:
        await worker.stop()  # остановка не должна падать и не должна зависать

    ctx = received[0]
    assert ctx.event_type == "user.registered"
    assert ctx.data["user_id"] == response.json()["id"]
    assert ctx.correlation_id == "e2e-correlation-1"
    async with app.state.resources.session_factory() as session:
        sent = await session.scalar(select(OutboxMessage.sent_at))
        processed = await session.scalar(select(func.count()).select_from(ProcessedMessage))
    assert sent is not None
    assert processed == 1


# ----------------------------------------------------------------------------------------- события без получателя
async def test_event_without_subscriber_does_not_block_other_events(
    app: FastAPI, channel: AbstractChannel, publisher: Publisher
) -> None:
    """Событие, для которого нет очереди (подписчик ещё не развёрнут), откладывается и не задерживает остальные."""
    spec = make_spec()
    await bind_queue(channel, spec)
    await add_events(
        app, "nobody.listens.yet", 1
    )  # самое старое: именно оно первым попадает в пачку
    await asyncio.sleep(0.05)
    await add_events(app, spec.routing_keys[0], 2)
    relay = make_relay(app, publisher)

    sent = await relay.run_once()

    assert sent == 2  # обычные события ушли, несмотря на застрявшее перед ними
    await drain(channel, spec.name, 2)
    async with app.state.resources.session_factory() as session:
        stuck = (
            await session.scalars(select(OutboxMessage).where(OutboxMessage.sent_at.is_(None)))
        ).one()
    assert stuck.routing_key == "nobody.listens.yet"
    assert stuck.attempts == 1
    assert stuck.last_error
    assert "нет получателя" in stuck.last_error
    assert stuck.next_attempt_at > stuck.created_at  # следующая попытка отложена

    assert await relay.run_once() == 0  # пока пауза не прошла, его не трогаем


async def test_deferred_event_is_delivered_once_a_subscriber_appears(
    app: FastAPI, channel: AbstractChannel, publisher: Publisher
) -> None:
    spec = make_spec()
    await add_events(app, spec.routing_keys[0], 1)
    relay = make_relay(app, publisher)
    assert await relay.run_once() == 0  # очереди ещё нет: событие отложено

    await bind_queue(channel, spec)  # подписчик развернули
    async with app.state.resources.engine.begin() as conn:  # и пауза истекла
        await conn.execute(text("UPDATE outbox SET next_attempt_at = now() - interval '1 second'"))

    assert await relay.run_once() == 1
    assert len(await drain(channel, spec.name, 1)) == 1
