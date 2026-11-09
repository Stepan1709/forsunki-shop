"""Смена статусов заказа на реальной БД: повторы, гонки и случайные последовательности (Hypothesis)."""

import asyncio
import contextlib
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule, run_state_machine_as_test
from sqlalchemy import select, text

from app.core.config import Settings
from app.core.errors import AppError
from app.core.outbox import OutboxEventSink, OutboxMessage
from app.main import create_app
from app.modules.cart.repository import CartRepository
from app.modules.orders.models import Order
from app.modules.orders.service import OrderService
from app.modules.orders.state import (
    STOCK_RELEASING,
    InvalidTransitionError,
    OrderStatus,
    can_transition,
)
from app.modules.users.models import User


async def make_order(
    app: FastAPI, client: httpx.AsyncClient, headers: dict[str, str], injector_id: int, qty: int = 2
) -> uuid.UUID:
    await client.put(f"/cart/items/{injector_id}", json={"quantity": qty}, headers=headers)
    response = await client.post(
        "/orders", headers={**headers, "Idempotency-Key": str(uuid.uuid4())}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def transition(app: FastAPI, order_id: uuid.UUID, target: OrderStatus) -> bool:
    async with app.state.resources.session_factory() as session:
        service = OrderService(session, app.state.settings, OutboxEventSink(session))
        return await service.apply_transition(order_id, target)


async def stock(app: FastAPI, injector_id: int) -> int:
    async with app.state.resources.engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT stock FROM injectors WHERE id = :id"), {"id": injector_id}
                )
            ).scalar_one()
        )


async def event_names(app: FastAPI) -> list[str]:
    async with app.state.resources.session_factory() as session:
        return list(
            await session.scalars(
                select(OutboxMessage.routing_key)
                .where(
                    OutboxMessage.routing_key.like("order.%"),
                    OutboxMessage.routing_key != "order.expire.delay",
                )
                .order_by(OutboxMessage.created_at)
            )
        )


async def test_payment_is_applied_once_and_keeps_the_stock_reserved(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    order_id = await make_order(app, client, customer_headers, item)

    first = await transition(app, order_id, OrderStatus.PAID)
    second = await transition(app, order_id, OrderStatus.PAID)  # то же событие пришло повторно

    assert (first, second) == (True, False)
    async with app.state.resources.session_factory() as session:
        order = await session.get(Order, order_id)
    assert order is not None
    assert order.status == OrderStatus.PAID
    assert order.paid_at is not None
    assert await stock(app, item) == 3  # оплаченный товар остаётся проданным
    assert await event_names(app) == ["order.created", "order.paid"]  # событие оплаты ровно одно


@pytest.mark.parametrize(
    "target", [OrderStatus.EXPIRED, OrderStatus.CANCELLED, OrderStatus.PAYMENT_FAILED]
)
async def test_expiry_cancel_and_failed_payment_return_stock_exactly_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
    target: OrderStatus,
) -> None:
    item = catalog["bosch_new"]
    order_id = await make_order(app, client, customer_headers, item)
    assert await stock(app, item) == 3

    assert await transition(app, order_id, target) is True
    assert await transition(app, order_id, target) is False  # повтор не возвращает товар второй раз

    assert await stock(app, item) == 5


async def test_invalid_transitions_are_rejected_and_change_nothing(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    order_id = await make_order(app, client, customer_headers, item)
    await transition(app, order_id, OrderStatus.EXPIRED)

    # Поздняя оплата истёкшего заказа не должна «оживить» его и забрать товар, который уже вернулся на склад.
    with pytest.raises(InvalidTransitionError):
        await transition(app, order_id, OrderStatus.PAID)

    async with app.state.resources.session_factory() as session:
        order = await session.get(Order, order_id)
    assert order is not None
    assert order.status == OrderStatus.EXPIRED
    assert await stock(app, item) == 5


async def test_unknown_order_is_not_found(app: FastAPI) -> None:
    with pytest.raises(AppError) as error:
        await transition(app, uuid.uuid4(), OrderStatus.PAID)

    assert error.value.status_code == 404


async def test_concurrent_cancel_and_expiry_have_one_winner_and_stock_returns_once(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    order_id = await make_order(app, client, customer_headers, item, qty=3)

    targets = [OrderStatus.CANCELLED, OrderStatus.EXPIRED] * 4
    results = await asyncio.gather(
        *(transition(app, order_id, t) for t in targets), return_exceptions=True
    )

    winners = [r for r in results if r is True]
    assert len(winners) == 1  # статус сменил ровно один
    assert all(
        r is False or isinstance(r, InvalidTransitionError) for r in results if r is not True
    )
    assert await stock(app, item) == 5  # товар вернулся один раз (не 5 + 3 * N)
    names = await event_names(app)
    assert names.count("order.cancelled") + names.count("order.expired") == 1


# ------------------------------------------------------------------------------------ Hypothesis на реальной БД
def test_random_order_lifecycle_keeps_stock_and_statuses_consistent(
    infra_settings: Settings, migrated_db: None
) -> None:
    """Случайные заказы и смены статусов над настоящей БД сверяются с простой моделью.

    После каждого шага проверяется: остаток в БД равен модельному, он неотрицателен, статусы совпадают,
    а остаток + зарезервированное + проданное равно начальному запасу.
    """
    loop = asyncio.new_event_loop()
    app = create_app(infra_settings)
    stack = contextlib.AsyncExitStack()
    loop.run_until_complete(stack.enter_async_context(app.router.lifespan_context(app)))

    def run(coro: Any) -> Any:
        return loop.run_until_complete(coro)

    user_id = uuid.UUID(int=0)  # реальный id задаётся при создании Machine (nonlocal)
    injectors = (1, 2)
    initial = 6

    async def reset() -> uuid.UUID:
        async with app.state.resources.engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE users, injector_engines, injectors, engines, manufacturers, outbox,"
                    " processed_messages RESTART IDENTITY CASCADE"
                )
            )
            await conn.execute(text("INSERT INTO manufacturers (name, slug) VALUES ('B', 'b')"))
            for n in injectors:
                await conn.execute(
                    text(
                        "INSERT INTO injectors (oem_number, oem_normalized, title, description,"
                        " manufacturer_id, type, condition, price_kopecks, stock) VALUES"
                        " (:o, :o, 'T', '', 1, 'common_rail', 'new', 100000, :s)"
                    ).bindparams(o=f"OEM{n}", s=initial)
                )
        async with app.state.resources.session_factory() as session:
            user = User(email="p@example.com", password_hash="x")
            session.add(user)
            await session.commit()
            return user.id

    class Machine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            nonlocal user_id
            user_id = run(reset())
            self.model_stock = dict.fromkeys(injectors, initial)
            self.orders: list[tuple[uuid.UUID, dict[int, int], OrderStatus]] = []

        @rule(
            q1=st.integers(min_value=0, max_value=4),
            q2=st.integers(min_value=0, max_value=4),
        )
        def checkout(self, q1: int, q2: int) -> None:
            wanted = {1: q1, 2: q2}
            wanted = {k: v for k, v in wanted.items() if v > 0}

            async def go() -> uuid.UUID | None:
                async with app.state.resources.session_factory() as session:
                    cart = CartRepository(session)
                    await cart.clear(user_id)
                    for injector_id, qty in wanted.items():
                        await cart.upsert(user_id, injector_id, qty)
                    await session.commit()
                    service = OrderService(session, app.state.settings, OutboxEventSink(session))
                    try:
                        result = await service.checkout(user_id, str(uuid.uuid4()))
                    except AppError:
                        return None
                    return uuid.UUID(result.body["id"])

            order_id = run(go())
            affordable = bool(wanted) and all(self.model_stock[i] >= q for i, q in wanted.items())
            assert (order_id is not None) == affordable
            if order_id is not None:
                for i, q in wanted.items():
                    self.model_stock[i] -= q
                self.orders.append((order_id, wanted, OrderStatus.PENDING_PAYMENT))

        @rule(
            index=st.integers(min_value=0, max_value=50),
            new_status=st.sampled_from(list(OrderStatus)),
        )
        def change_status(self, index: int, new_status: OrderStatus) -> None:
            if not self.orders:
                return
            position = index % len(self.orders)
            order_id, quantities, current = self.orders[position]
            if current == new_status:
                assert run(transition(app, order_id, new_status)) is False
            elif can_transition(current, new_status):
                assert run(transition(app, order_id, new_status)) is True
                self.orders[position] = (order_id, quantities, new_status)
                if new_status in STOCK_RELEASING:
                    for i, q in quantities.items():
                        self.model_stock[i] += q
            else:
                with pytest.raises(InvalidTransitionError):
                    run(transition(app, order_id, new_status))

        @invariant()
        def database_matches_model(self) -> None:
            for injector_id in injectors:
                real = run(stock(app, injector_id))
                assert real == self.model_stock[injector_id] >= 0

            async def statuses() -> dict[uuid.UUID, OrderStatus]:
                async with app.state.resources.session_factory() as session:
                    rows = await session.execute(select(Order.id, Order.status))
                    return {r.id: r.status for r in rows}

            real_statuses = run(statuses())
            for order_id, _, status in self.orders:
                assert real_statuses[order_id] == status
            held = sum(
                q
                for _, quantities, status in self.orders
                if status in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAID)
                for q in quantities.values()
            )
            assert sum(self.model_stock.values()) + held == initial * len(injectors)

    try:
        run_state_machine_as_test(
            Machine,
            settings=settings(
                max_examples=12,
                stateful_step_count=12,
                deadline=None,
                suppress_health_check=list(HealthCheck),
            ),
        )
    finally:
        loop.run_until_complete(stack.aclose())
        loop.close()
