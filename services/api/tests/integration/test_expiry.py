"""Истечение неоплаченных заказов: очередь с задержкой (TTL + DLX) и страховочный обход. Реальный RabbitMQ и Postgres."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.config import Settings
from app.core.messaging.consumer import Consumer, Subscription
from app.core.messaging.publisher import Publisher
from app.core.messaging.topology import (
    DelayQueueSpec,
    QueueSpec,
    declare_delay_queue,
    declare_exchanges,
)
from app.core.outbox import OutboxEventSink, OutboxMessage
from app.modules.orders.models import Order
from app.modules.orders.service import OrderService
from app.modules.orders.state import OrderStatus
from app.worker.payment_handlers import make_expire_order_handler
from app.worker.relay import OutboxRelay
from app.worker.sweeper import ExpirySweeper
from tests.integration.conftest import create_user


async def make_order(
    client: httpx.AsyncClient, headers: dict[str, str], injector_id: int, qty: int = 2
) -> dict[str, Any]:
    await client.put(f"/cart/items/{injector_id}", json={"quantity": qty}, headers=headers)
    response = await client.post(
        "/orders", headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def status_of(app: FastAPI, order: dict[str, Any]) -> OrderStatus:
    async with app.state.resources.session_factory() as session:
        row = await session.get(Order, uuid.UUID(order["id"]))
        assert row is not None
        status: OrderStatus = row.status
        return status


async def stock(app: FastAPI, injector_id: int) -> int:
    async with app.state.resources.engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT stock FROM injectors WHERE id = :id"), {"id": injector_id}
                )
            ).scalar_one()
        )


async def set_status(app: FastAPI, order: dict[str, Any], target: OrderStatus) -> None:
    async with app.state.resources.session_factory() as session:
        service = OrderService(session, app.state.settings, OutboxEventSink(session))
        await service.apply_transition(uuid.UUID(order["id"]), target)


async def outbox_keys(app: FastAPI) -> list[str]:
    async with app.state.resources.session_factory() as session:
        return list(
            await session.scalars(
                select(OutboxMessage.routing_key).order_by(OutboxMessage.created_at)
            )
        )


# ------------------------------------------------------------------------------------ очередь с задержкой
async def start_delay_pipeline(
    app: FastAPI, amqp: Any, publisher: Publisher, ttl: float
) -> tuple[DelayQueueSpec, Consumer, OutboxRelay]:
    """Собирает ту же цепочку, что в воркере, но с уникальными именами и коротким TTL."""
    suffix = uuid.uuid4().hex[:8]
    delay = DelayQueueSpec(
        name=f"test.delay.{suffix}",
        bind_key=f"test.delay.{suffix}.in",
        dead_letter_key=f"test.expire.{suffix}",
        ttl_seconds=ttl,
    )
    channel = await amqp.channel()
    events, _ = await declare_exchanges(channel)
    await declare_delay_queue(channel, delay, events)
    spec = QueueSpec(f"test.expire.q.{suffix}", (delay.dead_letter_key,), (0.2,))
    consumer = Consumer(
        Subscription(spec, make_expire_order_handler(app.state.settings)),
        amqp,
        app.state.resources.session_factory,
        publisher,
        default_prefetch=5,
        shutdown_timeout=5,
    )
    await consumer.start()
    relay = OutboxRelay(
        app.state.resources.session_factory, publisher, batch_size=50, poll_interval=0.05
    )
    return delay, consumer, relay


async def schedule_expiry(app: FastAPI, delay: DelayQueueSpec, order: dict[str, Any]) -> None:
    async with app.state.resources.session_factory() as session:
        await OutboxEventSink(session).publish(delay.bind_key, {"order_id": order["id"]})
        await session.commit()


async def test_unpaid_order_expires_after_delay_and_stock_returns(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    amqp: Any,
    publisher: Publisher,
) -> None:
    item = catalog["bosch_new"]
    order = await make_order(client, customer_headers, item)
    assert await stock(app, item) == 3
    delay, consumer, relay = await start_delay_pipeline(app, amqp, publisher, ttl=1.0)
    try:
        await schedule_expiry(app, delay, order)
        await relay.run_once()  # событие уходит в очередь с TTL 1 с
        assert (
            await status_of(app, order) == OrderStatus.PENDING_PAYMENT
        )  # пока срок не вышел, заказ жив

        status = OrderStatus.PENDING_PAYMENT
        async with asyncio.timeout(15):
            while status == OrderStatus.PENDING_PAYMENT:
                await asyncio.sleep(0.1)
                status = await status_of(app, order)
    finally:
        await consumer.stop()

    assert status == OrderStatus.EXPIRED
    assert await stock(app, item) == 5  # резерв снят, товар снова доступен другим
    assert "order.expired" in await outbox_keys(app)


async def test_paid_order_is_not_expired_when_the_delayed_message_arrives(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    amqp: Any,
    publisher: Publisher,
) -> None:
    item = catalog["bosch_new"]
    order = await make_order(client, customer_headers, item)
    delay, consumer, relay = await start_delay_pipeline(app, amqp, publisher, ttl=1.0)
    try:
        await schedule_expiry(app, delay, order)
        await relay.run_once()
        await set_status(app, order, OrderStatus.PAID)  # покупатель успел оплатить
        await asyncio.sleep(2.5)  # сообщение об истечении уже пришло и обработано
    finally:
        await consumer.stop()

    assert await status_of(app, order) == OrderStatus.PAID  # истечение ничего не сломало
    assert await stock(app, item) == 3
    assert "order.expired" not in await outbox_keys(app)


async def test_expiry_message_delivered_twice_returns_stock_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    amqp: Any,
    publisher: Publisher,
) -> None:
    item = catalog["bosch_new"]
    order = await make_order(client, customer_headers, item)
    delay, consumer, relay = await start_delay_pipeline(app, amqp, publisher, ttl=0.5)
    try:
        await schedule_expiry(app, delay, order)
        await schedule_expiry(app, delay, order)  # дубль события в outbox (разные message_id)
        await relay.run_once()
        await asyncio.sleep(2.5)
    finally:
        await consumer.stop()

    assert await status_of(app, order) == OrderStatus.EXPIRED
    assert await stock(app, item) == 5  # не 7: возврат выполнен один раз
    assert (await outbox_keys(app)).count("order.expired") == 1


# ------------------------------------------------------------------------------------ страховочный обход
async def test_sweeper_closes_overdue_orders_even_if_the_message_was_lost(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    app_settings: Settings,
) -> None:
    item = catalog["bosch_new"]
    overdue = await make_order(client, customer_headers, item, qty=2)
    _, other = await create_user(app, "other@example.com")
    fresh = await make_order(client, other, catalog["denso_new"], qty=1)
    paid_user_order = await make_order(client, other, catalog["bosch_pd"], qty=1)
    await set_status(app, paid_user_order, OrderStatus.PAID)
    async with app.state.resources.engine.begin() as conn:  # у первого заказа срок вышел давно
        await conn.execute(
            text("UPDATE orders SET expires_at = now() - interval '10 minutes' WHERE id = :id"),
            {"id": overdue["id"]},
        )
    sweeper = ExpirySweeper(app.state.resources.session_factory, app_settings)

    closed = await sweeper.run_once()
    again = await sweeper.run_once()

    assert (closed, again) == (1, 0)
    assert await status_of(app, overdue) == OrderStatus.EXPIRED
    assert await stock(app, item) == 5
    assert await status_of(app, fresh) == OrderStatus.PENDING_PAYMENT  # срок ещё не вышел
    assert await status_of(app, paid_user_order) == OrderStatus.PAID


@pytest.fixture
def app_settings(app: FastAPI) -> Settings:
    settings: Settings = app.state.settings
    return settings.model_copy(update={"expiry_sweep_grace_seconds": 0.0})


async def test_sweepers_running_in_parallel_do_not_double_release_stock(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    app_settings: Settings,
) -> None:
    item = catalog["bosch_new"]
    order = await make_order(client, customer_headers, item, qty=3)
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(text("UPDATE orders SET expires_at = now() - interval '1 hour'"))
    sweepers = [ExpirySweeper(app.state.resources.session_factory, app_settings) for _ in range(4)]

    results = await asyncio.gather(*(s.run_once() for s in sweepers))

    assert sum(results) == 1
    assert await status_of(app, order) == OrderStatus.EXPIRED
    assert await stock(app, item) == 5


async def test_checkout_schedules_expiry_event(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(client, customer_headers, catalog["bosch_new"])

    async with app.state.resources.session_factory() as session:
        payload = (
            await session.scalars(
                select(OutboxMessage.payload).where(
                    OutboxMessage.routing_key == "order.expire.delay"
                )
            )
        ).one()
    assert payload == {"order_id": order["id"]}
    ttl_minutes = app.state.settings.order_payment_ttl_minutes
    assert ttl_minutes == 60
    async with app.state.resources.session_factory() as session:
        row = await session.get(Order, uuid.UUID(order["id"]))
    assert row is not None
    assert 59 * 60 < (row.expires_at - row.created_at).total_seconds() <= 60 * 60 + 1
