"""Страницы: главная, магазин, корзина, заказы, ремонт, ошибки."""

import re
import uuid

import httpx
from fastapi import FastAPI

from tests.conftest import csrf_of, get_token
from tests.fake_api import FakeApi

HTMX = {"HX-Request": "true"}


# ------------------------------------------------------------------------------------ главная и магазин
async def test_home_renders_with_featured_and_nav(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert "Новинки каталога" in response.text
    assert 'href="/shop"' in response.text
    assert 'href="/repair"' in response.text
    assert "unpkg" not in response.text
    assert "cdn." not in response.text


async def test_home_survives_catalog_failure(client: httpx.AsyncClient, api: FakeApi) -> None:
    from httpx import Response

    api.overrides["/catalog/injectors"] = lambda call: Response(500, json={"detail": "boom"})
    response = await client.get("/")
    assert response.status_code == 200
    assert "Новинки каталога" not in response.text


async def test_shop_list_full_page_and_htmx_partial(
    client: httpx.AsyncClient, api: FakeApi
) -> None:
    full = await client.get("/shop")
    assert full.status_code == 200
    assert "<html" in full.text
    assert "Форсунка Bosch" in full.text

    partial = await client.get("/shop?manufacturer=bosch", headers=HTMX)
    assert "<html" not in partial.text  # для htmx приходит только блок результатов
    assert "Форсунка Bosch" in partial.text
    assert api.called("GET", "/catalog/injectors")[-1].params["manufacturer"] == "bosch"


async def test_shop_escapes_html_from_catalog(client: httpx.AsyncClient, api: FakeApi) -> None:
    from tests.fake_api import INJECTORS

    INJECTORS[2]["is_active"] = True
    try:
        response = await client.get("/shop")
    finally:
        INJECTORS[2]["is_active"] = False
    assert "<b>095000</b>" not in response.text
    assert "&lt;b&gt;095000&lt;/b&gt;" in response.text


async def test_shop_short_query_gives_hint_and_does_not_call_search(
    client: httpx.AsyncClient, api: FakeApi
) -> None:
    response = await client.get("/shop?q=ab")
    assert "не менее 3 символов" in response.text
    assert "q" not in api.called("GET", "/catalog/injectors")[-1].params


async def test_shop_load_more_returns_only_next_cards(
    client: httpx.AsyncClient, api: FakeApi
) -> None:
    # Фиктивный api отдаёт курсор при limit=1; сайт запрашивает 12, поэтому проверяем сам путь «ещё».
    response = await client.get("/shop?cursor=c2&manufacturer=bosch", headers=HTMX)
    assert response.status_code == 200
    assert "<html" not in response.text
    assert api.called("GET", "/catalog/injectors")[-1].params["cursor"] == "c2"


async def test_shop_bad_cursor_shows_notice_instead_of_error(client: httpx.AsyncClient) -> None:
    response = await client.get("/shop?cursor=bad")
    assert response.status_code == 200
    assert "Проверьте параметры поиска" in response.text


async def test_card_page_and_unknown_id(client: httpx.AsyncClient) -> None:
    card = await client.get("/shop/1")
    assert card.status_code == 200
    assert "Mercedes-Benz OM651" in card.text
    assert "45 000,00 ₽" in card.text
    missing = await client.get("/shop/999")
    assert missing.status_code == 404
    assert "Не найдено" in missing.text


async def test_unknown_page_is_friendly_404(client: httpx.AsyncClient) -> None:
    response = await client.get("/no-such-page")
    assert response.status_code == 404
    assert "На главную" in response.text


async def test_api_down_shows_503_page(client: httpx.AsyncClient, api: FakeApi) -> None:
    api.down = True
    response = await client.get("/shop/1")
    assert response.status_code == 503
    assert "временно недоступен" in response.text


# ------------------------------------------------------------------------------------ корзина и заказ
async def test_anonymous_add_to_cart_redirects_htmx_to_login(client: httpx.AsyncClient) -> None:
    token = csrf_of((await client.get("/shop/1")).text)
    response = await client.post(
        "/cart/add",
        data={"injector_id": "1", "quantity": "1"},
        headers={**HTMX, "X-CSRF-Token": token, "HX-Current-URL": "http://test/shop/1"},
    )
    assert response.status_code == 204
    assert response.headers["HX-Redirect"] == "/login?next=/shop/1"


async def test_add_to_cart_accumulates_and_signals_badge(
    buyer: httpx.AsyncClient, api: FakeApi
) -> None:
    token = csrf_of((await buyer.get("/shop/1")).text)
    for _ in range(2):
        response = await buyer.post(
            "/cart/add",
            data={"injector_id": "1", "quantity": "2"},
            headers={**HTMX, "X-CSRF-Token": token},
        )
    assert response.headers["HX-Trigger"] == "cart-changed"
    assert "всего 4 шт." in response.text
    badge = await buyer.get("/cart/badge")
    assert ">4<" in badge.text


async def test_add_more_than_stock_shows_message(buyer: httpx.AsyncClient) -> None:
    token = csrf_of((await buyer.get("/shop/1")).text)
    response = await buyer.post(
        "/cart/add",
        data={"injector_id": "1", "quantity": "50"},
        headers={**HTMX, "X-CSRF-Token": token},
    )
    assert "Недостаточно товара" in response.text


async def test_cart_page_change_and_remove(buyer: httpx.AsyncClient) -> None:
    token = csrf_of((await buyer.get("/shop/1")).text)
    await buyer.post(
        "/cart/add",
        data={"injector_id": "1", "quantity": "1"},
        headers={**HTMX, "X-CSRF-Token": token},
    )
    page = await buyer.get("/cart")
    assert "Форсунка Bosch" in page.text
    assert "Оформить заказ" in page.text

    changed = await buyer.post(
        "/cart/items/1", data={"quantity": "3"}, headers={**HTMX, "X-CSRF-Token": token}
    )
    assert "135 000,00 ₽" in changed.text

    too_many = await buyer.post(
        "/cart/items/1", data={"quantity": "9"}, headers={**HTMX, "X-CSRF-Token": token}
    )
    assert "Недостаточно товара" in too_many.text

    removed = await buyer.post("/cart/items/1/remove", headers={**HTMX, "X-CSRF-Token": token})
    assert "В корзине пока пусто" in removed.text


async def test_checkout_creates_order_once_per_form_key(
    buyer: httpx.AsyncClient, api: FakeApi
) -> None:
    token = csrf_of((await buyer.get("/shop/1")).text)
    await buyer.post(
        "/cart/add",
        data={"injector_id": "1", "quantity": "2"},
        headers={**HTMX, "X-CSRF-Token": token},
    )
    page = (await buyer.get("/cart")).text
    key = re.search(r'name="idem" value="([^"]+)"', page)
    assert key
    first = await buyer.post("/checkout", data={"csrf_token": token, "idem": key.group(1)})
    assert first.status_code == 303
    assert first.headers["location"].startswith("/orders/")
    posted = api.called("POST", "/orders")[-1]
    assert posted.headers["idempotency-key"] == key.group(
        1
    )  # тот же ключ при повторной отправке формы
    assert posted.headers["authorization"].startswith("Bearer ")


async def test_checkout_with_empty_cart_shows_error(buyer: httpx.AsyncClient) -> None:
    token = csrf_of((await buyer.get("/cart")).text)
    response = await buyer.post("/checkout", data={"csrf_token": token, "idem": str(uuid.uuid4())})
    assert response.status_code == 409
    assert "Корзина пуста" in response.text


async def _place_order(buyer: httpx.AsyncClient) -> str:
    token = csrf_of((await buyer.get("/shop/1")).text)
    await buyer.post(
        "/cart/add",
        data={"injector_id": "1", "quantity": "1"},
        headers={**HTMX, "X-CSRF-Token": token},
    )
    response = await buyer.post("/checkout", data={"csrf_token": token, "idem": str(uuid.uuid4())})
    return str(response.headers["location"])


async def test_order_page_polls_while_pending_and_shows_pay_link(
    buyer: httpx.AsyncClient, api: FakeApi
) -> None:
    location = await _place_order(buyer)
    page = await buyer.get(location)
    assert "Ожидает оплаты" in page.text
    assert 'hx-trigger="every 3s"' in page.text
    assert "http://pay.test/p1" in page.text
    assert "Перейти к оплате" in page.text

    api.payment = {"status": "CREATING"}
    creating = await buyer.get(f"{location}/status", headers=HTMX)
    assert "Готовим платёж" in creating.text


async def test_order_page_stops_polling_after_payment(
    buyer: httpx.AsyncClient, api: FakeApi
) -> None:
    location = await _place_order(buyer)
    api.order_status = "PAID"
    status = await buyer.get(f"{location}/status", headers=HTMX)
    assert "Оплачен" in status.text
    assert "hx-trigger" not in status.text  # опрос прекращается
    assert (
        api.called("GET", location + "/payment") == []
    )  # платёж запрашивали только пока заказ ждёт оплаты


async def test_cancel_order(buyer: httpx.AsyncClient, api: FakeApi) -> None:
    location = await _place_order(buyer)
    token = csrf_of((await buyer.get(location)).text)
    response = await buyer.post(f"{location}/cancel", data={"csrf_token": token})
    assert response.status_code == 303
    page = await buyer.get(location)
    assert "Заказ отменён" in page.text
    assert api.called("POST", location + "/cancel")[-1].headers["idempotency-key"]


async def test_profile_lists_orders(buyer: httpx.AsyncClient) -> None:
    await _place_order(buyer)
    page = await buyer.get("/profile")
    assert "buyer@example.com" in page.text
    assert "Мои заказы" in page.text
    assert "45 000,00 ₽" in page.text


async def test_foreign_order_is_404(buyer: httpx.AsyncClient) -> None:
    response = await buyer.get(f"/orders/{uuid.uuid4()}")
    assert response.status_code == 404


# ------------------------------------------------------------------------------------ ремонт
async def test_repair_page_has_prices_contacts_and_honeypot(client: httpx.AsyncClient) -> None:
    page = await client.get("/repair")
    assert "Примерные цены" in page.text
    assert "+7 000 000-00-00" in page.text
    assert 'name="website"' in page.text
    assert "aria-hidden" in page.text


async def test_repair_submit_passes_idempotency_key_and_honeypot(
    client: httpx.AsyncClient, api: FakeApi
) -> None:
    page = (await client.get("/repair")).text
    token, idem = csrf_of(page), re.search(r'name="idem" value="([^"]+)"', page).group(1)  # type: ignore[union-attr]
    data = {
        "csrf_token": token,
        "idem": idem,
        "name": "Иван",
        "phone": "+7 900 000-00-00",
        "email": "",
        "vehicle": "Sprinter OM651",
        "injector": "",
        "description": "Двигатель троит на холодную",
        "website": "",
    }
    response = await client.post("/repair", data=data, headers=HTMX)
    assert "Заявка принята" in response.text
    call = api.called("POST", "/repair-requests")[-1]
    assert call.headers["idempotency-key"] == idem
    assert call.json["email"] is None
    assert call.json["website"] == ""

    no_js = await client.post("/repair", data=data)  # без htmx: страница целиком
    assert no_js.status_code == 200
    assert "<html" in no_js.text
    assert "Заявка принята" in no_js.text


async def test_repair_validation_error_is_shown_next_to_form(client: httpx.AsyncClient) -> None:
    token = await get_token(client)
    response = await client.post(
        "/repair",
        data={
            "csrf_token": token,
            "idem": str(uuid.uuid4()),
            "name": "Иван",
            "phone": "",
            "email": "",
            "vehicle": "Sprinter",
            "injector": "",
            "description": "Двигатель троит на холодную",
        },
    )
    assert response.status_code == 422
    assert "Проверьте поля формы" in response.text
    assert "Укажите телефон или email" in response.text
    assert "Двигатель троит на холодную" in response.text  # введённые данные не потеряны


async def test_repair_rate_limit_message(client: httpx.AsyncClient) -> None:
    token = await get_token(client)
    response = await client.post(
        "/repair",
        data={
            "csrf_token": token,
            "idem": str(uuid.uuid4()),
            "name": "Бот",
            "phone": "+7 900 000-00-00",
            "email": "",
            "vehicle": "Sprinter",
            "injector": "",
            "description": "Двигатель троит на холодную",
        },
    )
    assert response.status_code == 429
    assert "Слишком много попыток" in response.text


# ------------------------------------------------------------------------------------ здоровье
async def test_health_probes(client: httpx.AsyncClient, api: FakeApi, app: FastAPI) -> None:
    assert (await client.get("/health/live")).json() == {"status": "ok"}
    ready = await client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"redis": "ok", "api": "ok"}
    api.down = True
    degraded = await client.get("/health/ready")
    assert degraded.status_code == 503
    assert degraded.json()["checks"]["api"] == "fail"
