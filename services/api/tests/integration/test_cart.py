import httpx
from fastapi import FastAPI
from sqlalchemy import text

from tests.integration.conftest import create_user


async def cart(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, object]:
    response = await client.get("/cart", headers=headers)
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


async def test_cart_requires_authentication(client: httpx.AsyncClient) -> None:
    assert (await client.get("/cart")).status_code == 401
    assert (await client.put("/cart/items/1", json={"quantity": 1})).status_code == 401


async def test_empty_cart(client: httpx.AsyncClient, customer_headers: dict[str, str]) -> None:
    body = await cart(client, customer_headers)

    assert body == {"items": [], "total_kopecks": 0, "can_checkout": False}


async def test_add_change_and_remove_items(
    client: httpx.AsyncClient, customer_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    first, second = catalog["bosch_new"], catalog["denso_new"]

    await client.put(f"/cart/items/{first}", json={"quantity": 2}, headers=customer_headers)
    response = await client.put(
        f"/cart/items/{second}", json={"quantity": 1}, headers=customer_headers
    )

    body = response.json()
    assert [i["injector_id"] for i in body["items"]] == sorted([first, second])
    assert body["total_kopecks"] == 3 * 4_500_000  # цена каталога 45 000 руб.
    assert body["can_checkout"] is True

    # PUT устанавливает количество, а не прибавляет: повтор запроса даёт тот же результат.
    again = await client.put(f"/cart/items/{first}", json={"quantity": 2}, headers=customer_headers)
    assert again.json() == body
    changed = await client.put(
        f"/cart/items/{first}", json={"quantity": 4}, headers=customer_headers
    )
    assert next(i for i in changed.json()["items"] if i["injector_id"] == first)["quantity"] == 4

    assert (
        await client.delete(f"/cart/items/{first}", headers=customer_headers)
    ).status_code == 204
    assert (
        await client.delete(f"/cart/items/{first}", headers=customer_headers)
    ).status_code == 204  # удаление отсутствующей позиции не ошибка
    assert len((await cart(client, customer_headers))["items"]) == 1  # type: ignore[arg-type]

    await client.delete("/cart", headers=customer_headers)
    assert (await cart(client, customer_headers))["items"] == []


async def test_cannot_add_more_than_stock_or_unknown_or_hidden_item(
    client: httpx.AsyncClient, customer_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    item = catalog["bosch_new"]  # на складе 5

    too_many = await client.put(
        f"/cart/items/{item}", json={"quantity": 6}, headers=customer_headers
    )
    unknown = await client.put("/cart/items/999999", json={"quantity": 1}, headers=customer_headers)
    hidden = await client.put(
        f"/cart/items/{catalog['hidden']}", json={"quantity": 1}, headers=customer_headers
    )

    assert too_many.status_code == 409
    assert unknown.status_code == 404
    assert hidden.status_code == 404


async def test_quantity_must_be_between_1_and_99(
    client: httpx.AsyncClient, customer_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    url = f"/cart/items/{catalog['bosch_new']}"

    for bad in (0, -1, 100):
        assert (
            await client.put(url, json={"quantity": bad}, headers=customer_headers)
        ).status_code == 422


async def test_cart_marks_items_that_became_unavailable(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    item = catalog["bosch_new"]
    await client.put(f"/cart/items/{item}", json={"quantity": 3}, headers=customer_headers)
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(text("UPDATE injectors SET stock = 1 WHERE id = :id"), {"id": item})

    body = await cart(client, customer_headers)

    line = body["items"][0]  # type: ignore[index]
    assert line["available"] is False  # в корзине 3, на складе 1
    assert body["can_checkout"] is False


async def test_carts_of_different_users_are_isolated(
    app: FastAPI,
    client: httpx.AsyncClient,
    customer_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    _, other = await create_user(app, "other@example.com")
    await client.put(
        f"/cart/items/{catalog['bosch_new']}", json={"quantity": 1}, headers=customer_headers
    )

    assert (await cart(client, other))["items"] == []
