"""Сквозной сценарий через сайт (`make up`): тот же путь, что проходит покупатель в браузере.

Запуск: `make e2e`. Главная -> регистрация -> магазин -> товар -> корзина -> заказ -> оплата на странице провайдера ->
статус «Оплачен» -> заказ в истории профиля. Проверяется и то, что в cookie только id сессии.
"""

import os
import re
import sys
import time
import uuid

import httpx

# Адрес сайта: по умолчанию dev (make e2e); для prod на этой машине WEB_URL=https://localhost E2E_VERIFY_TLS=0 (make prod-e2e).
WEB = os.environ.get("WEB_URL", "http://localhost:8000")
VERIFY = os.environ.get("E2E_VERIFY_TLS", "1") != "0"  # самоподписанный сертификат локального prod
PASSWORD = "e2e-pass-12345"  # noqa: S105  (тестовый пароль одноразового пользователя)
HTMX = {"HX-Request": "true"}


def step(message: str) -> None:
    print(f"  ok  {message}")


def fail(message: str) -> None:
    print(f"FAIL  {message}")
    sys.exit(1)


def csrf(html: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    if not found:
        fail("на странице нет csrf_token")
        raise SystemExit(1)
    return found.group(1)


def main() -> None:
    client = httpx.Client(base_url=WEB, timeout=15, verify=VERIFY)

    home = client.get("/")
    if home.status_code != 200 or "Перейти в магазин" not in home.text:
        fail(f"главная не открылась: {home.status_code}")
    step("главная открывается")

    # Регистрация (после неё пользователь сразу вошёл).
    email = f"web-{uuid.uuid4().hex[:10]}@example.com"
    token = csrf(client.get("/register").text)
    response = client.post(
        "/register",
        data={"email": email, "password": PASSWORD, "password2": PASSWORD, "csrf_token": token},
    )
    if response.status_code != 303:
        fail(f"регистрация не удалась: {response.status_code}")
    cookie = client.cookies.get("sid") or ""
    if "." in cookie or "eyJ" in cookie:
        fail("в cookie похоже на токен, а не на случайный id")
    step("регистрация и вход, в cookie только id сессии")
    if WEB.startswith("https"):
        set_cookie = response.headers.get("set-cookie", "")
        if "Secure" not in set_cookie or "HttpOnly" not in set_cookie:
            fail(f"cookie сессии без Secure/HttpOnly: {set_cookie}")
        step("cookie сессии помечена Secure и HttpOnly")
        for path in ("/metrics", "/health/ready"):
            if client.get(path).status_code != 404:
                fail(f"{path} доступен снаружи")
        if "max-age" not in home.headers.get("strict-transport-security", ""):
            fail("нет заголовка HSTS")
        step("служебные пути закрыты, заголовки безопасности на месте")

    # Магазин и карточка.
    shop = client.get("/shop", params={"condition": "new"})
    ids = list(dict.fromkeys(re.findall(r'href="/shop/(\d+)"', shop.text)))
    if not ids:
        fail("в магазине нет товаров (выполните make seed)")
    # Берём первый товар, который есть в наличии: часть каталога могла быть распродана предыдущими запусками.
    chosen = ""
    product = shop
    for candidate in ids:
        page = client.get(f"/shop/{candidate}")
        if "В корзину" in page.text:
            chosen, product = candidate, page
            break
    if not chosen:
        fail("в первых товарах каталога нет остатка (make seed COUNT=2000)")
    token = csrf(product.text)
    step("магазин и карточка товара открываются")

    # Корзина.
    added = client.post(
        "/cart/add",
        data={"injector_id": chosen, "quantity": "1"},
        headers={**HTMX, "X-CSRF-Token": token},
    )
    if "Добавлено в корзину" not in added.text:
        fail("товар не добавлен в корзину")
    cart = client.get("/cart")
    key = re.search(r'name="idem" value="([^"]+)"', cart.text)
    if not key:
        fail("в корзине нет формы оформления")
        raise SystemExit(1)
    step("товар в корзине")

    # Заказ (дважды с тем же ключом: заказ должен быть один).
    first = client.post("/checkout", data={"csrf_token": token, "idem": key.group(1)})
    again = client.post("/checkout", data={"csrf_token": token, "idem": key.group(1)})
    order_path = first.headers.get("location", "")
    if first.status_code != 303 or not order_path.startswith("/orders/"):
        fail(f"заказ не оформлен: {first.status_code}")
    if again.status_code == 303 and again.headers.get("location") != order_path:
        fail("повторная отправка формы создала второй заказ")
    step("заказ оформлен, повторная отправка формы не создаёт второй")

    # Платёж: воркер создаёт его асинхронно, страница опрашивает статус.
    pay_url = ""
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        panel = client.get(f"{order_path}/status", headers=HTMX).text
        found = re.search(r'href="(http[^"]*/pay/[^"]+)"', panel)
        if found:
            pay_url = found.group(1)
            break
        time.sleep(0.7)
    if not pay_url:
        fail("не дождались ссылки на оплату")
    step("воркер создал платёж, на странице заказа появилась кнопка оплаты")

    httpx.post(f"{pay_url}/confirm", follow_redirects=True, timeout=15, verify=VERIFY)
    paid = False
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        panel = client.get(f"{order_path}/status", headers=HTMX).text
        if "Оплачен" in panel:
            paid = "hx-trigger" not in panel  # после оплаты опрос на странице прекращается
            break
        time.sleep(0.7)
    if not paid:
        fail("заказ не стал оплаченным или страница продолжает опрос")
    step("оплата на странице провайдера, заказ «Оплачен», опрос остановлен")

    profile = client.get("/profile")
    number = order_path.rsplit("/", 1)[1][:8].upper()
    if number not in profile.text or "Оплачен" not in profile.text:
        fail("оплаченный заказ не виден в истории профиля")
    step("заказ виден в истории профиля")

    # Выход.
    token = csrf(profile.text)
    client.post("/logout", data={"csrf_token": token})
    if client.get("/profile").status_code != 303:
        fail("после выхода профиль остался доступен")
    step("выход закрывает сессию")
    print("Все проверки пройдены")


if __name__ == "__main__":
    main()
