"""Обновление токенов (в том числе параллельное), админка товаров и вспомогательные функции."""

import asyncio

import httpx
from fastapi import FastAPI

from app.templating import dt, money, parse_rubles, rubles_input
from tests.conftest import csrf_of
from tests.fake_api import FakeApi

HTMX = {"HX-Request": "true"}


# ------------------------------------------------------------------------------------ токены
async def test_expired_access_token_is_refreshed_transparently(
    buyer: httpx.AsyncClient, api: FakeApi, app: FastAPI
) -> None:
    keys = await app.state.redis.keys("web:sess:*")
    import json

    data = json.loads(await app.state.redis.get(keys[0]))
    api.expired_access.add(data["access"])  # api больше не принимает этот токен

    response = await buyer.get("/cart")
    assert response.status_code == 200
    assert api.refresh_count == 1
    # Новые токены сохранены, следующий запрос обходится без повторного обновления.
    await buyer.get("/cart")
    assert api.refresh_count == 1


async def test_parallel_requests_refresh_token_only_once(
    buyer: httpx.AsyncClient, api: FakeApi, app: FastAPI
) -> None:
    """refresh-токен одноразовый: второе предъявление api счёл бы кражей. Параллельные запросы делят одно обновление."""
    import json

    keys = await app.state.redis.keys("web:sess:*")
    data = json.loads(await app.state.redis.get(keys[0]))
    api.expired_access.add(data["access"])

    responses = await asyncio.gather(*(buyer.get("/cart") for _ in range(6)))
    assert [r.status_code for r in responses] == [200] * 6
    assert api.refresh_count == 1


async def test_failed_refresh_sends_user_to_login_and_drops_tokens(
    buyer: httpx.AsyncClient, api: FakeApi, app: FastAPI
) -> None:
    import json

    keys = await app.state.redis.keys("web:sess:*")
    data = json.loads(await app.state.redis.get(keys[0]))
    api.expired_access.add(data["access"])
    api.refresh_valid = False

    response = await buyer.get("/cart")
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert (await buyer.get("/profile")).status_code == 303  # вход снова нужен


# ------------------------------------------------------------------------------------ админка
async def test_admin_pages_require_admin_role(
    buyer: httpx.AsyncClient, client: httpx.AsyncClient
) -> None:
    response = await buyer.get("/admin/products")
    assert response.status_code == 403
    assert "нет прав" in response.text
    anonymous = httpx.AsyncClient(transport=client._transport, base_url="http://test")
    async with anonymous:
        assert (await anonymous.get("/admin/products")).status_code == 303


async def test_admin_menu_visible_only_to_admin(buyer: httpx.AsyncClient, api: FakeApi) -> None:
    assert "/admin/products" not in (await buyer.get("/")).text


async def test_admin_list_includes_hidden_products(admin: httpx.AsyncClient) -> None:
    page = await admin.get("/admin/products")
    assert page.status_code == 200
    assert "В продаже" in page.text
    assert "Скрыт" in page.text
    assert "/admin/products" in (await admin.get("/")).text


async def test_admin_create_converts_rubles_to_kopecks(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    token = csrf_of((await admin.get("/admin/products/new")).text)
    response = await admin.post(
        "/admin/products",
        data={
            "csrf_token": token,
            "oem_number": "0 445 120 077",
            "title": "Bosch 0445120077",
            "manufacturer_id": "1",
            "type": "common_rail",
            "condition": "new",
            "price": "45 000,50",
            "stock": "3",
            "description": "Описание",
            "engines": "Cummins ISF 2.8\n\n  Renault K9K  ",
            "is_active": "1",
        },
    )
    assert response.status_code == 303
    sent = api.called("POST", "/admin/catalog/injectors")[-1].json
    assert sent["price_kopecks"] == 4_500_050
    assert sent["engines"] == ["Cummins ISF 2.8", "Renault K9K"]
    assert sent["is_active"] is True
    assert sent["stock"] == 3


async def test_admin_invalid_price_is_reported_without_calling_api(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    token = csrf_of((await admin.get("/admin/products/new")).text)
    response = await admin.post(
        "/admin/products",
        data={
            "csrf_token": token,
            "oem_number": "X-1234",
            "title": "Товар",
            "manufacturer_id": "1",
            "type": "common_rail",
            "condition": "new",
            "price": "дорого",
            "stock": "-1",
        },
    )
    assert response.status_code == 422
    assert "Введите цену в рублях" in response.text
    assert "целое число" in response.text
    assert api.called("POST", "/admin/catalog/injectors") == []


async def test_admin_duplicate_article_error_is_shown(admin: httpx.AsyncClient) -> None:
    token = csrf_of((await admin.get("/admin/products/new")).text)
    response = await admin.post(
        "/admin/products",
        data={
            "csrf_token": token,
            "oem_number": "DUP-001",
            "title": "Товар",
            "manufacturer_id": "1",
            "type": "common_rail",
            "condition": "new",
            "price": "100",
            "stock": "1",
        },
    )
    assert response.status_code == 422
    assert "уже существует" in response.text


async def test_admin_edit_and_hide_product(admin: httpx.AsyncClient, api: FakeApi) -> None:
    page = await admin.get("/admin/products/1")
    assert "45000.00" in page.text
    assert "Mercedes-Benz OM651 2.1 CDI" in page.text
    token = csrf_of(page.text)
    response = await admin.post(
        "/admin/products/1",
        data={
            "csrf_token": token,
            "oem_number": "0 445 110 183",
            "title": "Новое название",
            "manufacturer_id": "1",
            "type": "common_rail",
            "condition": "new",
            "price": "46000",
            "stock": "7",
        },  # галочка is_active не передана: товар скрывается
    )
    assert response.status_code == 303
    sent = api.called("PATCH", "/admin/catalog/injectors/1")[-1].json
    assert sent["is_active"] is False
    assert sent["price_kopecks"] == 4_600_000


async def test_admin_image_upload_limits(admin: httpx.AsyncClient, api: FakeApi) -> None:
    token = csrf_of((await admin.get("/admin/products/1")).text)
    big = await admin.post(
        "/admin/products/1/image",
        data={"csrf_token": token},
        files={
            "file": ("big.png", b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024 + 10), "image/png")
        },
    )
    assert big.status_code == 303
    assert (
        api.called("PUT", "/catalog/injectors/1/image") == []
    )  # слишком большой файл до api не дошёл
    assert "не больше 2 МБ" in (await admin.get("/admin/products/1")).text

    ok = await admin.post(
        "/admin/products/1/image",
        data={"csrf_token": token},
        files={"file": ("a.png", b"\x89PNG\r\n\x1a\n" + b"0" * 100, "image/png")},
    )
    assert ok.status_code == 303
    assert api.called("PUT", "/catalog/injectors/1/image")
    assert "Картинка загружена" in (await admin.get("/admin/products/1")).text


# ------------------------------------------------------------------------------------ утилиты
def test_money_format() -> None:
    assert money(4_500_000) == "45 000,00 ₽"
    assert money(5) == "0,05 ₽"


def test_parse_rubles() -> None:
    assert parse_rubles("45000") == 4_500_000
    assert parse_rubles("45 000,5") == 4_500_050
    assert parse_rubles("0,10") == 10
    for bad in ("", "abc", "-5", "1,234", "1e3x"):
        assert parse_rubles(bad) is None


def test_rubles_input_roundtrip() -> None:
    assert rubles_input(4_500_050) == "45000.50"
    assert parse_rubles(rubles_input(4_500_050)) == 4_500_050


def test_dt_uses_moscow_time() -> None:
    assert dt("2026-11-10T10:00:00Z") == "10.11.2026 13:00"
