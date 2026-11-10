"""Админский API каталога: доступ, создание, правка, сброс кэша, скрытие товара."""

from typing import Any

import httpx

BASE = "/admin/catalog/injectors"


def new_injector(manufacturer_id: int, **overrides: Any) -> dict[str, Any]:
    return {
        "oem_number": "0 445 120 077",
        "title": "Bosch 0445120077",
        "description": "Для Cummins ISF",
        "manufacturer_id": manufacturer_id,
        "type": "common_rail",
        "condition": "new",
        "price_kopecks": 3_000_000,
        "stock": 4,
        "engines": ["Cummins ISF 2.8"],
        **overrides,
    }


async def manufacturer_id(client: httpx.AsyncClient) -> int:
    items = (await client.get("/catalog/manufacturers")).json()
    return int(items[0]["id"])


async def test_admin_api_requires_admin_role(
    client: httpx.AsyncClient, customer_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    assert (await client.get(BASE)).status_code == 401
    assert (await client.get(BASE, headers=customer_headers)).status_code == 403
    body = new_injector(await manufacturer_id(client))
    assert (await client.post(BASE, json=body, headers=customer_headers)).status_code == 403
    assert (
        await client.patch(
            f"{BASE}/{catalog['bosch_new']}", json={"stock": 1}, headers=customer_headers
        )
    ).status_code == 403


async def test_create_injector_appears_in_public_catalog(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    response = await client.post(
        BASE, json=new_injector(await manufacturer_id(client)), headers=admin_headers
    )
    assert response.status_code == 201
    created = response.json()
    assert created["is_active"] is True
    assert [e["name"] for e in created["engines"]] == ["Cummins ISF 2.8"]

    public = await client.get(f"/catalog/injectors/{created['id']}")
    assert public.status_code == 200
    assert public.json()["price_kopecks"] == 3_000_000
    # Поиск по нормализованному артикулу находит новый товар.
    found = await client.get("/catalog/injectors", params={"q": "0445120077"})
    assert [i["id"] for i in found.json()["items"]] == [created["id"]]


async def test_duplicate_oem_and_condition_is_conflict(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    mid = await manufacturer_id(client)
    body = new_injector(mid)
    assert (await client.post(BASE, json=body, headers=admin_headers)).status_code == 201
    again = await client.post(
        BASE, json={**body, "oem_number": "0445-120-077"}, headers=admin_headers
    )
    assert again.status_code == 409
    # Тот же артикул в другом состоянии это другой товар.
    other = await client.post(
        BASE, json={**body, "condition": "remanufactured"}, headers=admin_headers
    )
    assert other.status_code == 201


async def test_create_with_unknown_manufacturer_and_invalid_values(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    mid = await manufacturer_id(client)
    unknown = await client.post(BASE, json=new_injector(99999), headers=admin_headers)
    assert unknown.status_code == 404
    for patch in ({"price_kopecks": -1}, {"stock": -5}, {"title": ""}, {"type": "rocket"}):
        response = await client.post(BASE, json=new_injector(mid, **patch), headers=admin_headers)
        assert response.status_code == 422, patch


async def test_update_changes_only_sent_fields_and_resets_cache(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    injector_id = catalog["bosch_new"]
    before = (await client.get(f"/catalog/injectors/{injector_id}")).json()
    # Второй запрос идёт из кэша: проверим, что после правки он обновится.
    assert (await client.get(f"/catalog/injectors/{injector_id}")).headers["X-Cache"] == "HIT"

    response = await client.patch(
        f"{BASE}/{injector_id}",
        json={"price_kopecks": 5_100_000, "stock": 9},
        headers=admin_headers,
    )
    assert response.status_code == 200
    after = (await client.get(f"/catalog/injectors/{injector_id}")).json()
    assert after["price_kopecks"] == 5_100_000
    assert after["stock"] == 9
    assert after["title"] == before["title"]
    assert after["description"] == before["description"]
    assert after["engines"] == before["engines"]


async def test_update_engines_and_title(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    injector_id = catalog["bosch_new"]
    response = await client.patch(
        f"{BASE}/{injector_id}",
        json={"title": "Новое название", "engines": ["Renault K9K", "Renault K9K", "Opel Z17"]},
        headers=admin_headers,
    )
    assert response.status_code == 200
    card = response.json()
    assert card["title"] == "Новое название"
    assert sorted(e["name"] for e in card["engines"]) == ["Opel Z17", "Renault K9K"]


async def test_deactivated_injector_is_hidden_from_public_but_visible_to_admin(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    injector_id = catalog["denso_new"]
    await client.patch(f"{BASE}/{injector_id}", json={"is_active": False}, headers=admin_headers)
    assert (await client.get(f"/catalog/injectors/{injector_id}")).status_code == 404
    public_ids = {i["id"] for i in (await client.get("/catalog/injectors")).json()["items"]}
    assert injector_id not in public_ids

    admin_items = (await client.get(BASE, headers=admin_headers)).json()["items"]
    row = next(i for i in admin_items if i["id"] == injector_id)
    assert row["is_active"] is False
    # Снятый с продажи (hidden из фикстуры) тоже виден администратору.
    assert catalog["hidden"] in {i["id"] for i in admin_items}

    await client.patch(f"{BASE}/{injector_id}", json={"is_active": True}, headers=admin_headers)
    assert (await client.get(f"/catalog/injectors/{injector_id}")).status_code == 200


async def test_update_unknown_injector_and_manufacturer(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    assert (
        await client.patch(f"{BASE}/99999", json={"stock": 1}, headers=admin_headers)
    ).status_code == 404
    response = await client.patch(
        f"{BASE}/{catalog['bosch_new']}", json={"manufacturer_id": 99999}, headers=admin_headers
    )
    assert response.status_code == 404


async def test_update_to_existing_oem_and_condition_is_conflict(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    # bosch_reman -> new при том же артикуле совпадёт с bosch_new.
    response = await client.patch(
        f"{BASE}/{catalog['bosch_reman']}", json={"condition": "new"}, headers=admin_headers
    )
    assert response.status_code == 409
