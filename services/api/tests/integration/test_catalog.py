import httpx
from fastapi import FastAPI

from app.core.cache import Cache
from app.modules.catalog.models import Injector, InjectorCondition, InjectorType


async def list_ids(client: httpx.AsyncClient, **params: object) -> list[int]:
    response = await client.get("/catalog/injectors", params=params)  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


# --- Список и фильтры ---


async def test_list_returns_only_active_items_in_id_order(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    ids = await list_ids(client)

    assert ids == sorted(ids)
    assert catalog["hidden"] not in ids
    assert len(ids) == 4


async def test_filter_by_manufacturer_type_and_condition(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    assert await list_ids(client, manufacturer="denso") == [catalog["denso_new"]]
    assert await list_ids(client, type="pump_injector") == [catalog["bosch_pd"]]
    assert await list_ids(client, condition="remanufactured") == [catalog["bosch_reman"]]
    assert await list_ids(client, manufacturer="bosch", condition="new") == [
        catalog["bosch_new"],
        catalog["bosch_pd"],
    ]


async def test_unknown_filter_value_is_validation_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/catalog/injectors", params={"type": "rocket"})

    assert response.status_code == 422


# --- Поиск ---


async def test_search_by_oem_ignores_spaces_dashes_and_case(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    expected = [catalog["bosch_new"], catalog["bosch_reman"]]

    assert await list_ids(client, q="0 445 110 183") == expected
    assert await list_ids(client, q="0445-110183") == expected
    assert await list_ids(client, q="445110") == expected  # часть артикула


async def test_search_by_engine_applicability(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    assert await list_ids(client, q="om651") == [catalog["bosch_new"], catalog["bosch_reman"]]
    assert await list_ids(client, q="CBAB") == [catalog["denso_new"], catalog["bosch_pd"]]


async def test_search_wildcards_are_treated_literally(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    # "%%%" не должно превращаться в «найти всё»: спецсимволы LIKE экранируются.
    assert await list_ids(client, q="%%%") == []
    assert await list_ids(client, q="___") == []


async def test_too_short_search_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/catalog/injectors", params={"q": "ab"})

    assert response.status_code == 422


# --- Пагинация ---


async def test_cursor_pagination_walks_every_item_exactly_once(
    app: FastAPI, client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    async with app.state.resources.session_factory() as session:
        for i in range(25):
            session.add(
                Injector(
                    oem_number=f"X{i:03d}",
                    oem_normalized=f"X{i:03d}",
                    title=f"Тест {i}",
                    description="",
                    manufacturer_id=1,
                    type=InjectorType.COMMON_RAIL,
                    condition=InjectorCondition.NEW,
                    price_kopecks=100,
                    stock=1,
                )
            )
        await session.commit()

    collected: list[int] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, object] = {"limit": 10}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get("/catalog/injectors", params=params)).json()  # type: ignore[arg-type]
        collected += [item["id"] for item in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert pages == 3  # 29 активных товаров по 10 на страницу
    assert len(collected) == len(set(collected)) == 29
    assert collected == sorted(collected)


async def test_invalid_cursor_and_limits_are_rejected(client: httpx.AsyncClient) -> None:
    assert (await client.get("/catalog/injectors", params={"cursor": "###"})).status_code == 422
    assert (await client.get("/catalog/injectors", params={"limit": 101})).status_code == 422
    assert (await client.get("/catalog/injectors", params={"limit": 0})).status_code == 422


# --- Карточка и производители ---


async def test_card_contains_engines_and_manufacturer(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    response = await client.get(f"/catalog/injectors/{catalog['bosch_new']}")

    assert response.status_code == 200
    body = response.json()
    assert body["manufacturer"]["slug"] == "bosch"
    assert body["engines"][0]["name"] == "Mercedes-Benz OM651 2.1 CDI"
    assert body["price_kopecks"] == 4_500_000
    assert body["image_url"] is None


async def test_card_of_missing_or_hidden_item_is_404(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    assert (await client.get("/catalog/injectors/999999")).status_code == 404
    hidden = await client.get(f"/catalog/injectors/{catalog['hidden']}")
    assert hidden.status_code == 404
    assert hidden.headers["content-type"] == "application/problem+json"


async def test_manufacturers_are_listed_by_name(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    body = (await client.get("/catalog/manufacturers")).json()

    assert [m["slug"] for m in body] == ["bosch", "denso"]


# --- Кэш ---


async def test_second_identical_request_is_served_from_cache(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    first = await client.get("/catalog/injectors", params={"manufacturer": "bosch"})
    second = await client.get("/catalog/injectors", params={"manufacturer": "bosch"})
    other = await client.get("/catalog/injectors", params={"manufacturer": "denso"})

    assert first.headers["x-cache"] == "MISS"
    assert second.headers["x-cache"] == "HIT"
    assert second.json() == first.json()
    assert other.headers["x-cache"] == "MISS"  # другие параметры - другой ключ


async def test_card_is_cached_and_cache_is_reset_by_version_bump(
    app: FastAPI, client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    url = f"/catalog/injectors/{catalog['bosch_new']}"
    await client.get(url)
    assert (await client.get(url)).headers["x-cache"] == "HIT"

    await Cache(app.state.resources.redis).bump_version()

    assert (await client.get(url)).headers["x-cache"] == "MISS"


async def test_catalog_works_when_redis_is_down(
    app: FastAPI, client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    import dataclasses

    from redis.asyncio import Redis

    broken = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True)
    app.state.resources = dataclasses.replace(app.state.resources, redis=broken)

    response = await client.get("/catalog/injectors")
    await broken.aclose()

    assert response.status_code == 200  # Redis не источник истины: отвечаем из БД
    assert response.headers["x-cache"] == "MISS"
    assert len(response.json()["items"]) == 4
