"""Сквозной сценарий на запущенном стеке (`make up`): регистрация, корзина, заказ, оплата в заглушке, проверка статусов.

Запуск: `make e2e`. Проходит по HTTP тот же путь, что и покупатель, и проверяет итог на каждом шаге.
Сценарии: успешная оплата (с повтором webhook'а провайдером) и отказ платежа (товар возвращается на склад).
"""

import sys
import time
import uuid
from pathlib import Path

import httpx

API = "http://localhost:8001"
STUB = "http://localhost:8002"
PASSWORD = "e2e-pass-12345"  # noqa: S105  (тестовый пароль одноразового пользователя)
SECRETS = Path(__file__).resolve().parent.parent / "deploy" / "secrets" / "dev"


def step(message: str) -> None:
    print(f"  ok  {message}")


def fail(message: str) -> None:
    print(f"FAIL  {message}")
    sys.exit(1)


def wait_for(description: str, check, timeout: float = 40.0, interval: float = 0.5):  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(interval)
    fail(f"не дождались: {description}")


def main() -> None:
    api_key = (SECRETS / "payment_api_key").read_text().strip()
    client = httpx.Client(timeout=15)

    # 1. Регистрация и вход
    email = f"e2e-{uuid.uuid4().hex[:10]}@example.com"
    client.post(
        f"{API}/auth/register", json={"email": email, "password": PASSWORD}
    ).raise_for_status()
    tokens = client.post(f"{API}/auth/login", json={"email": email, "password": PASSWORD}).json()
    auth = {"Authorization": f"Bearer {tokens['access_token']}"}
    step("регистрация и вход")

    # 2. Товар в наличии
    listing = client.get(f"{API}/catalog/injectors", params={"limit": 50}).json()["items"]
    item = next((i for i in listing if i["stock"] >= 3), None)
    if item is None:
        fail("в каталоге нет товара с остатком >= 3 (выполните make seed)")

    def stock() -> int:
        client.post(
            f"{API}/debug/cache/reset"
        )  # остаток в кэше каталога приблизительный: сбрасываем для точной проверки
        return int(client.get(f"{API}/catalog/injectors/{item['id']}").json()["stock"])

    def place_order(quantity: int) -> dict:
        client.put(
            f"{API}/cart/items/{item['id']}", json={"quantity": quantity}, headers=auth
        ).raise_for_status()
        response = client.post(
            f"{API}/orders", headers={**auth, "Idempotency-Key": str(uuid.uuid4())}
        )
        if response.status_code != 201:
            fail(f"заказ не создан: {response.status_code} {response.text}")
        return response.json()

    def get_order(order_id: str) -> dict:
        return client.get(f"{API}/orders/{order_id}", headers=auth).json()

    def payment_of(order_id: str) -> dict:
        return client.get(f"{API}/orders/{order_id}/payment", headers=auth).json()

    def pay_url_of(order_id: str) -> str:
        payment = wait_for(
            "ссылка на оплату", lambda: (p := payment_of(order_id))["status"] == "PENDING" and p
        )
        return str(payment["pay_url"])

    # --- Сценарий 1: успешная оплата ---
    print("Сценарий 1: успешная оплата")
    before = stock()
    order = place_order(2)
    if order["status"] != "PENDING_PAYMENT":
        fail(f"неожиданный статус нового заказа: {order['status']}")
    step(f"заказ создан, сумма {order['total_kopecks'] / 100:.2f} руб.")
    if stock() != before - 2:
        fail("резерв остатка не выполнен")
    step("остаток зарезервирован")

    pay_url = pay_url_of(order["id"])
    page = client.get(pay_url)
    if "Оплатить" not in page.text:
        fail("на странице оплаты нет кнопки")
    step("воркер создал платёж, страница оплаты открывается")

    client.post(f"{pay_url}/confirm", follow_redirects=True)
    wait_for("статус PAID", lambda: get_order(order["id"])["status"] == "PAID")
    step("заглушка прислала webhook, заказ оплачен (PAID)")
    if payment_of(order["id"])["status"] != "SUCCEEDED":
        fail("платёж не SUCCEEDED")

    payment_id = pay_url.rsplit("/", 1)[1]
    resend = client.post(
        f"{STUB}/payments/{payment_id}/webhook/resend",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if not resend.json().get("delivered"):
        fail("повтор webhook'а не доставлен")
    time.sleep(1)
    if get_order(order["id"])["status"] != "PAID" or stock() != before - 2:
        fail("повторный webhook изменил состояние")
    step("повторный webhook провайдера учтён один раз (дубль отсечён)")

    # --- Сценарий 2: отказ платежа ---
    print("Сценарий 2: отказ платежа")
    before = stock()
    order = place_order(1)
    pay_url = pay_url_of(order["id"])
    client.post(f"{pay_url}/decline", follow_redirects=True)
    wait_for("статус PAYMENT_FAILED", lambda: get_order(order["id"])["status"] == "PAYMENT_FAILED")
    step("платёж отклонён, заказ PAYMENT_FAILED")
    if stock() != before:
        fail("товар не вернулся на склад")
    step("остаток вернулся на склад")

    print("Все проверки пройдены")


if __name__ == "__main__":
    main()
