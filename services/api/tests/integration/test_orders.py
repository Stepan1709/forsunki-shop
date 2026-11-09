import asyncio
import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import func, select, text

from app.core.outbox import OutboxMessage
from app.modules.orders.models import Order
from tests.integration.conftest import create_user


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


async def put_cart(
    client: httpx.AsyncClient, headers: dict[str, str], injector_id: int, quantity: int
) -> None:
    response = await client.put(
        f"/cart/items/{injector_id}", json={"quantity": quantity}, headers=headers
    )
    assert response.status_code == 200, response.text


async def stock(app: FastAPI, injector_id: int) -> int:
    async with app.state.resources.engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT stock FROM injectors WHERE id = :id"), {"id": injector_id}
                )
            ).scalar_one()
        )


async def count(app: FastAPI, model: Any) -> int:
    async with app.state.resources.session_factory() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def events(app: FastAPI) -> list[str]:
    async with app.state.resources.session_factory() as session:
        rows = await session.scalars(
            select(OutboxMessage.routing_key)
            .where(
                OutboxMessage.routing_key.like("order.%"),
                OutboxMessage.routing_key != "order.expire.delay",
            )
            .order_by(OutboxMessage.created_at)
        )
        return list(rows)


async def checkout(
    client: httpx.AsyncClient, headers: dict[str, str], idem: str | None = None
) -> httpx.Response:
    return await client.post(
        "/orders", headers={**headers, "Idempotency-Key": idem or str(uuid.uuid4())}
    )


# ------------------------------------------------------------------------------------ оформление
async def test_checkout_creates_order_reserves_stock_and_clears_cart(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    first, second = catalog["bosch_new"], catalog["denso_new"]
    await put_cart(client, customer_headers, first, 2)
    await put_cart(client, customer_headers, second, 1)

    response = await checkout(client, customer_headers)

    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "PENDING_PAYMENT"
    assert order["total_kopecks"] == 3 * 4_500_000
    assert order["currency"] == "RUB"
    assert {i["injector_id"]: i["quantity"] for i in order["items"]} == {first: 2, second: 1}
    assert await stock(app, first) == 3  # было 5
    assert await stock(app, second) == 4
    assert (await client.get("/cart", headers=customer_headers)).json()["items"] == []
    assert await events(app) == ["order.created"]


async def test_price_is_frozen_at_checkout(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    await put_cart(client, customer_headers, item, 1)
    order = (await checkout(client, customer_headers)).json()

    async with app.state.resources.engine.begin() as conn:  # админ поднял цену
        await conn.execute(
            text("UPDATE injectors SET price_kopecks = 9900000 WHERE id = :id"), {"id": item}
        )
    stored = (await client.get(f"/orders/{order['id']}", headers=customer_headers)).json()
    await put_cart(client, customer_headers, item, 1)
    new_cart = (await client.get("/cart", headers=customer_headers)).json()

    assert stored["total_kopecks"] == 4_500_000  # оформленный заказ не изменился
    assert stored["items"][0]["unit_price_kopecks"] == 4_500_000
    assert new_cart["total_kopecks"] == 9_900_000  # а новая корзина считается по новой цене


async def test_checkout_of_empty_cart_is_rejected(
    client: httpx.AsyncClient, customer_headers: dict[str, str]
) -> None:
    response = await checkout(client, customer_headers)

    assert response.status_code == 422


async def test_insufficient_stock_rejects_whole_order_without_side_effects(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    ok_item, short_item = catalog["bosch_new"], catalog["denso_new"]
    await put_cart(client, customer_headers, ok_item, 2)
    await put_cart(client, customer_headers, short_item, 2)
    async with (
        app.state.resources.engine.begin() as conn
    ):  # между корзиной и заказом товар разобрали
        await conn.execute(
            text("UPDATE injectors SET stock = 1 WHERE id = :id"), {"id": short_item}
        )
    idem = str(uuid.uuid4())

    response = await checkout(client, customer_headers, idem)

    assert response.status_code == 409
    assert await stock(app, ok_item) == 5  # резерв первой позиции откатился вместе с остальным
    assert await stock(app, short_item) == 1
    assert await count(app, Order) == 0
    assert await events(app) == []
    assert len((await client.get("/cart", headers=customer_headers)).json()["items"]) == 2
    # Неудачная попытка не запоминается: тот же ключ можно использовать снова, когда товар появится.
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(
            text("UPDATE injectors SET stock = 5 WHERE id = :id"), {"id": short_item}
        )
    assert (await checkout(client, customer_headers, idem)).status_code == 201


# ------------------------------------------------------------------------------------ идемпотентность
async def test_idempotency_key_is_required_and_validated(
    client: httpx.AsyncClient, customer_headers: dict[str, str]
) -> None:
    missing = await client.post("/orders", headers=customer_headers)
    short = await client.post("/orders", headers={**customer_headers, "Idempotency-Key": "short"})

    assert missing.status_code == 400
    assert short.status_code == 400


async def test_repeated_key_returns_the_same_order_and_does_not_repeat_the_action(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    await put_cart(client, customer_headers, item, 2)
    idem = str(uuid.uuid4())

    first = await checkout(client, customer_headers, idem)
    second = await checkout(client, customer_headers, idem)

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()  # тот же заказ
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert await count(app, Order) == 1
    assert await stock(app, item) == 3  # резерв выполнен один раз
    assert await events(app) == ["order.created"]  # и событие одно


async def test_same_key_of_different_users_does_not_collide(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    _, other = await create_user(app, "other@example.com")
    item = catalog["bosch_new"]
    await put_cart(client, customer_headers, item, 1)
    await put_cart(client, other, item, 1)
    idem = str(uuid.uuid4())

    a = await checkout(client, customer_headers, idem)
    b = await checkout(client, other, idem)

    assert a.status_code == b.status_code == 201
    assert a.json()["id"] != b.json()["id"]
    assert "idempotent-replayed" not in b.headers


async def test_key_cannot_be_reused_for_a_different_request(
    client: httpx.AsyncClient, customer_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    await put_cart(client, customer_headers, catalog["bosch_new"], 1)
    idem = str(uuid.uuid4())
    order = (await checkout(client, customer_headers, idem)).json()

    reuse = await client.post(
        f"/orders/{order['id']}/cancel", headers={**customer_headers, "Idempotency-Key": idem}
    )

    assert reuse.status_code == 422


async def test_parallel_requests_with_same_key_create_one_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    await put_cart(client, customer_headers, item, 2)
    idem = str(uuid.uuid4())

    responses = await asyncio.gather(*(checkout(client, customer_headers, idem) for _ in range(6)))

    assert {r.status_code for r in responses} == {201}
    assert len({r.json()["id"] for r in responses}) == 1  # все получили один и тот же заказ
    assert await count(app, Order) == 1
    assert await stock(app, item) == 3


async def test_parallel_checkouts_for_the_last_unit_sell_it_exactly_once(
    app: FastAPI, client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    item = catalog["bosch_new"]
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(text("UPDATE injectors SET stock = 1 WHERE id = :id"), {"id": item})
    buyers = []
    for i in range(8):
        _, headers = await create_user(app, f"buyer{i}@example.com")
        await put_cart(client, headers, item, 1)
        buyers.append(headers)

    responses = await asyncio.gather(*(checkout(client, h) for h in buyers))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [201] + [409] * 7  # заказ получил один, остальным отказ
    assert await stock(app, item) == 0  # и остаток не ушёл в минус
    assert await count(app, Order) == 1


# ------------------------------------------------------------------------------------ отмена
async def test_cancel_returns_stock_and_is_idempotent(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    await put_cart(client, customer_headers, item, 2)
    order = (await checkout(client, customer_headers)).json()
    assert await stock(app, item) == 3
    idem = str(uuid.uuid4())

    cancelled = await client.post(
        f"/orders/{order['id']}/cancel", headers={**customer_headers, "Idempotency-Key": idem}
    )
    replay = await client.post(
        f"/orders/{order['id']}/cancel", headers={**customer_headers, "Idempotency-Key": idem}
    )
    again = await client.post(  # другой ключ, заказ уже отменён
        f"/orders/{order['id']}/cancel", headers={**customer_headers, **key()}
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert replay.json() == cancelled.json()
    assert again.status_code == 200
    assert again.json()["status"] == "CANCELLED"
    assert await stock(app, item) == 5  # вернулось ровно одно резервирование, а не три
    assert await events(app) == ["order.created", "order.cancelled"]


async def test_paid_order_cannot_be_cancelled(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    from app.core.outbox import OutboxEventSink
    from app.modules.orders.service import OrderService
    from app.modules.orders.state import OrderStatus

    await put_cart(client, customer_headers, catalog["bosch_new"], 1)
    order = (await checkout(client, customer_headers)).json()
    async with app.state.resources.session_factory() as session:
        service = OrderService(session, app.state.settings, OutboxEventSink(session))
        await service.apply_transition(uuid.UUID(order["id"]), OrderStatus.PAID)

    response = await client.post(
        f"/orders/{order['id']}/cancel", headers={**customer_headers, **key()}
    )

    assert response.status_code == 409
    assert (await client.get(f"/orders/{order['id']}", headers=customer_headers)).json()[
        "status"
    ] == "PAID"


# ------------------------------------------------------------------------------------ доступ и история
async def test_user_cannot_see_or_cancel_someone_elses_order(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    _, intruder = await create_user(app, "intruder@example.com")
    await put_cart(client, customer_headers, catalog["bosch_new"], 1)
    order = (await checkout(client, customer_headers)).json()

    read = await client.get(f"/orders/{order['id']}", headers=intruder)
    cancel = await client.post(f"/orders/{order['id']}/cancel", headers={**intruder, **key()})
    listing = await client.get("/orders", headers=intruder)
    missing = await client.get(f"/orders/{uuid.uuid4()}", headers=intruder)

    assert read.status_code == 404  # 404, а не 403: существование чужого заказа не раскрывается
    assert cancel.status_code == 404
    assert missing.status_code == 404
    assert listing.json()["items"] == []
    assert (await client.get(f"/orders/{order['id']}", headers=customer_headers)).status_code == 200


async def test_orders_require_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/orders")).status_code == 401
    assert (await client.post("/orders", headers=key())).status_code == 401


async def test_history_is_newest_first_and_paginated(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    created = []
    for _ in range(5):
        await put_cart(client, customer_headers, catalog["denso_new"], 1)
        created.append((await checkout(client, customer_headers)).json()["id"])

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get("/orders", params=params, headers=customer_headers)).json()
        seen += [o["id"] for o in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert pages == 3  # 5 заказов по 2
    assert seen == list(reversed(created))  # от новых к старым, без дублей и пропусков
    bad = await client.get("/orders", params={"cursor": "###"}, headers=customer_headers)
    assert bad.status_code == 422
