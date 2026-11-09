"""Платежи: создание, webhook (подпись, дубли, сумма), поздняя оплата и гонки. Реальные Postgres и RabbitMQ."""

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.messaging.topology import QueueSpec
from app.core.outbox import OutboxEventSink, OutboxMessage
from app.modules.orders.models import Order
from app.modules.orders.service import OrderService
from app.modules.orders.state import OrderStatus
from app.modules.payments.gateway import GatewayTemporaryError
from app.modules.payments.models import Payment, PaymentStatus, ProviderEvent
from app.modules.payments.service import PaymentService
from app.worker.payment_handlers import make_create_payment_handler
from tests.helpers import WEBHOOK_SECRET
from tests.integration.conftest import create_user
from tests.integration.fakes import FakeGateway
from tests.integration.test_messaging import Harness, make_spec, wait_until


# ------------------------------------------------------------------------------------ помощники
async def make_order(
    app: FastAPI, client: httpx.AsyncClient, headers: dict[str, str], injector_id: int, qty: int = 2
) -> dict[str, Any]:
    await client.put(f"/cart/items/{injector_id}", json={"quantity": qty}, headers=headers)
    response = await client.post(
        "/orders", headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


async def create_payment(
    app: FastAPI, order: dict[str, Any], provider_payment_id: str = "pay-1", attempt: int = 1
) -> Payment:
    """Платёж у провайдера «уже создан»: строка в БД с известным id провайдера (как после работы воркера)."""
    async with app.state.resources.session_factory() as session:
        payment = Payment(
            order_id=uuid.UUID(order["id"]),
            attempt=attempt,
            status=PaymentStatus.PENDING,
            provider="stub",
            provider_payment_id=provider_payment_id,
            pay_url=f"http://pay/{provider_payment_id}",
            amount_kopecks=order["total_kopecks"],
            currency=order["currency"],
        )
        session.add(payment)
        await session.commit()
        return payment


def signed_event(
    order: dict[str, Any],
    *,
    kind: str = "payment.succeeded",
    event_id: str | None = None,
    payment_id: str = "pay-1",
    amount: int | None = None,
    currency: str | None = None,
    timestamp: int | None = None,
    secret: str = WEBHOOK_SECRET,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "event_id": event_id or uuid.uuid4().hex,
            "type": kind,
            "payment_id": payment_id,
            "order_id": order["id"],
            "amount_kopecks": order["total_kopecks"] if amount is None else amount,
            "currency": currency or order["currency"],
        }
    ).encode()
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return body, {"X-Stub-Signature": f"t={ts},v1={digest}", "Content-Type": "application/json"}


async def post_webhook(
    client: httpx.AsyncClient, body: bytes, headers: dict[str, str]
) -> httpx.Response:
    return await client.post("/payments/webhook", content=body, headers=headers)


async def order_status(app: FastAPI, order: dict[str, Any]) -> OrderStatus:
    async with app.state.resources.session_factory() as session:
        row = await session.get(Order, uuid.UUID(order["id"]))
        assert row is not None
        status: OrderStatus = row.status
        return status


async def payment_status(app: FastAPI, provider_payment_id: str = "pay-1") -> PaymentStatus:
    async with app.state.resources.session_factory() as session:
        result: PaymentStatus = (
            await session.scalars(
                select(Payment.status).where(Payment.provider_payment_id == provider_payment_id)
            )
        ).one()
        return result


async def stock(app: FastAPI, injector_id: int) -> int:
    async with app.state.resources.engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT stock FROM injectors WHERE id = :id"), {"id": injector_id}
                )
            ).scalar_one()
        )


async def events(app: FastAPI) -> list[str]:
    async with app.state.resources.session_factory() as session:
        rows = await session.scalars(
            select(OutboxMessage.routing_key)
            .where(
                OutboxMessage.routing_key != "order.expire.delay",
                OutboxMessage.routing_key.regexp_match("^(order|payment)\\."),
            )
            .order_by(OutboxMessage.created_at)
        )
        return list(rows)


async def transition(app: FastAPI, order: dict[str, Any], target: OrderStatus) -> bool:
    async with app.state.resources.session_factory() as session:
        service = OrderService(session, app.state.settings, OutboxEventSink(session))
        return await service.apply_transition(uuid.UUID(order["id"]), target)


# ------------------------------------------------------------------------------------ создание платежа
async def test_payment_is_created_once_for_a_repeated_event(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    gateway = FakeGateway()

    async def create() -> Payment | None:
        async with app.state.resources.session_factory() as session:
            service = PaymentService(session, app.state.settings, gateway, OutboxEventSink(session))
            payment = await service.create_for_order(uuid.UUID(order["id"]), "cid-1")
            await session.commit()
            return payment

    first = await create()
    second = await create()  # событие order.created пришло повторно

    assert first is not None
    assert second is not None
    assert second.id == first.id
    assert len(gateway.requests) == 1  # провайдера повторно не дёргали
    request = gateway.requests[0]
    assert request.idempotency_key == f"{order['id']}:1"  # ключ провайдера = заказ + номер попытки
    assert (request.amount_kopecks, request.currency) == (order["total_kopecks"], "RUB")
    assert first.pay_url
    assert first.provider_payment_id


async def test_payment_is_not_created_for_a_closed_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await transition(
        app, order, OrderStatus.CANCELLED
    )  # заказ отменили, пока событие ждало в очереди
    gateway = FakeGateway()

    async with app.state.resources.session_factory() as session:
        service = PaymentService(session, app.state.settings, gateway, OutboxEventSink(session))
        result = await service.create_for_order(uuid.UUID(order["id"]), None)

    assert result is None
    assert gateway.requests == []  # платить нечего: провайдера не трогаем


async def test_worker_creates_payment_and_retries_with_the_same_provider_key(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    harness: Harness,
    publisher: Any,
) -> None:
    """Провайдер временно недоступен: сообщение повторяется, платёж создаётся один раз, ключ провайдера тот же."""
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    gateway = FakeGateway()
    gateway.failures = [GatewayTemporaryError("провайдер недоступен")]
    spec = make_spec(delays=(0.2,))
    handler = make_create_payment_handler(app.state.settings, gateway)

    async with harness.running(spec, handler):
        await publisher.publish_event(
            spec.routing_keys[0],
            {"order_id": order["id"]},
            message_id=uuid.uuid4(),
            correlation_id="c",
        )
        await wait_until(lambda: len(gateway.requests) == 2, limit=15)
        await asyncio.sleep(0.3)

    assert len({r.idempotency_key for r in gateway.requests}) == 1
    async with app.state.resources.session_factory() as session:
        payments = list(await session.scalars(select(Payment)))
    assert len(payments) == 1  # откат первой попытки не оставил лишней строки
    assert payments[0].provider_payment_id is not None


async def test_order_payment_endpoint_shows_link_to_owner_only(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    _, intruder = await create_user(app, "intruder@example.com")
    url = f"/orders/{order['id']}/payment"

    creating = await client.get(url, headers=customer_headers)
    await create_payment(app, order)
    ready = await client.get(url, headers=customer_headers)
    foreign = await client.get(url, headers=intruder)
    anonymous = await client.get(url)

    assert creating.json() == {
        "status": "CREATING",
        "pay_url": None,
        "amount_kopecks": None,
        "currency": None,
    }
    assert ready.json()["status"] == "PENDING"
    assert ready.json()["pay_url"] == "http://pay/pay-1"
    assert foreign.status_code == 404  # чужой заказ не раскрывается
    assert anonymous.status_code == 401


# ------------------------------------------------------------------------------------ webhook
async def test_successful_payment_event_marks_order_paid(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await create_payment(app, order)
    body, headers = signed_event(order)

    response = await post_webhook(client, body, headers)

    assert response.status_code == 200
    assert response.json() == {"status": "processed"}
    assert await order_status(app, order) == OrderStatus.PAID
    assert await payment_status(app) == PaymentStatus.SUCCEEDED
    assert await stock(app, catalog["bosch_new"]) == 3  # оплаченный товар остаётся проданным
    assert await events(app) == ["order.created", "order.paid"]
    async with app.state.resources.session_factory() as session:
        paid_at = (await session.get(Order, uuid.UUID(order["id"]))).paid_at
    assert paid_at is not None


async def test_double_webhook_is_counted_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await create_payment(app, order)
    body, headers = signed_event(order, event_id="evt-same")

    first = await post_webhook(client, body, headers)
    second = await post_webhook(client, body, headers)  # провайдер прислал то же событие ещё раз

    assert first.json() == {"status": "processed"}
    assert second.status_code == 200  # 2xx, чтобы провайдер прекратил повторы
    assert second.json() == {"status": "duplicate"}
    assert (await events(app)).count("order.paid") == 1
    async with app.state.resources.session_factory() as session:
        assert (await session.scalar(select(func.count()).select_from(ProviderEvent))) == 1


async def test_second_event_about_an_already_paid_payment_changes_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await create_payment(app, order)
    await post_webhook(client, *signed_event(order))

    again = await post_webhook(client, *signed_event(order))  # новый event_id, тот же платёж

    assert again.json() == {"status": "ignored"}
    assert (await events(app)).count("order.paid") == 1
    assert await payment_status(app) == PaymentStatus.SUCCEEDED


async def test_parallel_identical_webhooks_apply_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await create_payment(app, order)
    body, headers = signed_event(order, event_id="evt-parallel")

    responses = await asyncio.gather(*(post_webhook(client, body, headers) for _ in range(6)))

    results = sorted(r.json()["status"] for r in responses)
    assert results == ["duplicate"] * 5 + ["processed"]
    assert (await events(app)).count("order.paid") == 1
    assert await order_status(app, order) == OrderStatus.PAID


async def test_parallel_success_and_failure_events_leave_consistent_state(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    """Провайдер прислал противоречивые события одновременно: побеждает одно, товар и статусы согласованы."""
    item = catalog["bosch_new"]
    order = await make_order(app, client, customer_headers, item)
    await create_payment(app, order)
    batch = [
        signed_event(order, kind="payment.succeeded"),
        signed_event(order, kind="payment.failed"),
        signed_event(order, kind="payment.succeeded"),
        signed_event(order, kind="payment.failed"),
    ]

    await asyncio.gather(*(post_webhook(client, b, h) for b, h in batch))

    status = await order_status(app, order)
    assert status in (OrderStatus.PAID, OrderStatus.PAYMENT_FAILED)
    if status == OrderStatus.PAID:
        assert await payment_status(app) == PaymentStatus.SUCCEEDED
        assert await stock(app, item) == 3
    else:
        assert await payment_status(app) == PaymentStatus.FAILED
        assert await stock(app, item) == 5  # товар вернулся ровно один раз


# ------------------------------------------------------------------------------------ подпись и содержимое
@pytest.mark.parametrize(
    "tamper",
    ["wrong_secret", "stale", "no_header", "garbage"],
)
async def test_invalid_signature_is_rejected_and_nothing_changes(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    tamper: str,
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await create_payment(app, order)
    if tamper == "wrong_secret":
        body, headers = signed_event(order, secret="чужой-секрет")
    elif tamper == "stale":
        body, headers = signed_event(order, timestamp=int(time.time()) - 3600)
    elif tamper == "no_header":
        body, _ = signed_event(order)
        headers = {"Content-Type": "application/json"}
    else:
        body, _ = signed_event(order)
        headers = {"X-Stub-Signature": "garbage"}

    response = await post_webhook(client, body, headers)

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert await order_status(app, order) == OrderStatus.PENDING_PAYMENT
    assert await payment_status(app) == PaymentStatus.PENDING
    async with app.state.resources.session_factory() as session:
        assert (await session.scalar(select(func.count()).select_from(ProviderEvent))) == 0


async def test_tampered_amount_in_body_breaks_the_signature(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await create_payment(app, order)
    body, headers = signed_event(order)
    forged = body.replace(str(order["total_kopecks"]).encode(), b"1")  # злоумышленник занизил сумму

    response = await post_webhook(client, forged, headers)

    assert response.status_code == 401
    assert await order_status(app, order) == OrderStatus.PENDING_PAYMENT


async def test_malformed_event_with_valid_signature_is_400(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    body = b'{"hello": "world"}'
    ts = int(time.time())
    digest = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()

    response = await post_webhook(client, body, {"X-Stub-Signature": f"t={ts},v1={digest}"})

    assert response.status_code == 400


@pytest.mark.parametrize("field", ["amount", "currency"])
async def test_amount_or_currency_mismatch_does_not_pay_the_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    field: str,
) -> None:
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await create_payment(app, order)
    kwargs: dict[str, Any] = {"amount": 100} if field == "amount" else {"currency": "USD"}
    body, headers = signed_event(order, **kwargs)

    response = await post_webhook(client, body, headers)

    assert response.json() == {"status": "rejected"}
    assert (
        await order_status(app, order) == OrderStatus.PENDING_PAYMENT
    )  # заказ не считается оплаченным
    assert await payment_status(app) == PaymentStatus.REVIEW_REQUIRED
    assert "payment.review_required" in await events(app)


async def test_event_for_unknown_payment_is_acknowledged_and_ignored(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    order = {"id": str(uuid.uuid4()), "total_kopecks": 100, "currency": "RUB"}

    response = await post_webhook(client, *signed_event(order, payment_id="never-created"))

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


async def test_failed_payment_cancels_order_and_returns_stock(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    order = await make_order(app, client, customer_headers, item)
    await create_payment(app, order)
    assert await stock(app, item) == 3

    response = await post_webhook(client, *signed_event(order, kind="payment.failed"))

    assert response.json() == {"status": "processed"}
    assert await order_status(app, order) == OrderStatus.PAYMENT_FAILED
    assert await payment_status(app) == PaymentStatus.FAILED
    assert await stock(app, item) == 5
    assert await events(app) == ["order.created", "order.payment_failed"]


# ------------------------------------------------------------------------------------ оплата после истечения
@pytest.mark.parametrize("closing", [OrderStatus.EXPIRED, OrderStatus.CANCELLED])
async def test_payment_after_expiry_or_cancel_requires_refund_and_does_not_revive_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    closing: OrderStatus,
) -> None:
    item = catalog["bosch_new"]
    order = await make_order(app, client, customer_headers, item)
    await create_payment(app, order)
    await transition(app, order, closing)
    assert await stock(app, item) == 5  # товар вернулся на склад и может достаться другому

    response = await post_webhook(client, *signed_event(order))  # деньги всё-таки пришли

    assert response.json() == {"status": "processed"}
    assert await order_status(app, order) == closing  # заказ не воскрес
    assert await payment_status(app) == PaymentStatus.REFUND_REQUIRED
    assert await stock(app, item) == 5  # и товар заново не зарезервирован
    names = await events(app)
    assert "payment.refund_required" in names
    assert "order.paid" not in names
    async with app.state.resources.session_factory() as session:
        reason = (
            await session.scalars(
                select(OutboxMessage.payload).where(
                    OutboxMessage.routing_key == "payment.refund_required"
                )
            )
        ).one()["reason"]
    assert reason == f"order_{closing.value.lower()}"


async def test_database_forbids_two_successful_payments_for_one_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    """Последний рубеж защиты от двойной оплаты: даже при ошибке в коде БД не пустит вторую успешную оплату."""
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    first = await create_payment(app, order, "pay-1")
    second = await create_payment(app, order, "pay-2", attempt=2)
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(
            text("UPDATE payments SET status = 'SUCCEEDED' WHERE id = :id"), {"id": first.id}
        )

    with pytest.raises(IntegrityError):
        async with app.state.resources.engine.begin() as conn:
            await conn.execute(
                text("UPDATE payments SET status = 'SUCCEEDED' WHERE id = :id"), {"id": second.id}
            )


async def test_second_payment_for_paid_order_requires_refund(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    """Покупатель оплатил заказ дважды (два платежа у провайдера): учтён один, второй уходит на возврат."""
    order = await make_order(app, client, customer_headers, catalog["bosch_new"])
    await create_payment(app, order, "pay-1")
    async with app.state.resources.session_factory() as session:
        session.add(
            Payment(
                order_id=uuid.UUID(order["id"]),
                attempt=2,
                status=PaymentStatus.PENDING,
                provider="stub",
                provider_payment_id="pay-2",
                pay_url="http://pay/2",
                amount_kopecks=order["total_kopecks"],
                currency="RUB",
            )
        )
        await session.commit()

    await post_webhook(client, *signed_event(order, payment_id="pay-1"))
    await post_webhook(client, *signed_event(order, payment_id="pay-2"))

    assert await payment_status(app, "pay-1") == PaymentStatus.SUCCEEDED
    assert await payment_status(app, "pay-2") == PaymentStatus.REFUND_REQUIRED
    assert (await events(app)).count("order.paid") == 1


# ------------------------------------------------------------------------------------ сквозной путь воркера
async def test_registered_order_gets_a_payment_through_outbox_and_worker(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    infra_settings: Any,
) -> None:
    """order.created -> outbox -> relay -> очередь -> обработчик воркера -> платёж -> ссылка в GET /orders/{id}/payment."""
    from app.core.messaging.consumer import Subscription
    from app.worker.runner import Worker

    gateway = FakeGateway()
    spec = QueueSpec(f"test.pay.{uuid.uuid4().hex[:8]}", ("order.created",), (0.2,))
    settings = infra_settings.model_copy(
        update={"relay_poll_interval_seconds": 0.05, "worker_heartbeat_path": None}
    )
    worker = Worker(
        settings,
        [Subscription(spec, make_create_payment_handler(settings, gateway))],
        delay_queues=[],
    )
    await worker.start()
    try:
        order = await make_order(app, client, customer_headers, catalog["bosch_new"])
        payment: dict[str, Any] = {}
        async with asyncio.timeout(15):
            while payment.get("status") != "PENDING":
                await asyncio.sleep(0.1)
                payment = (
                    await client.get(f"/orders/{order['id']}/payment", headers=customer_headers)
                ).json()
    finally:
        await worker.stop()

    assert payment["pay_url"].startswith("http://fake-pay/")
    assert payment["amount_kopecks"] == order["total_kopecks"]
    assert len(gateway.requests) == 1
