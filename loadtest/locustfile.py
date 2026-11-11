"""Сценарии нагрузочного теста Locust.

Два типа виртуальных пользователей (запускаются по отдельности: `locust ... CatalogUser` или `... BuyerUser`):

  * CatalogUser: смотрит каталог (список, фильтры, поиск, карточка). Режим CACHE_MODE=cold перед каждым обращением сбрасывает кэш
    служебным эндпоинтом, то есть эмулирует работу без кэша; CACHE_MODE=warm работает как обычно.
  * BuyerUser: полный путь покупки. Регистрация (один раз), затем в цикле: корзина, оформление заказа, ожидание платежа,
    оплата на странице заглушки, ожидание статуса PAID. Время от оформления до PAID пишется отдельной строкой «E2E checkout -> PAID».

Служебные запросы (регистрация, опрос статусов, сброс кэша) называются с префиксом «[...]» и в итоговой таблице не учитываются.
"""

# ruff: noqa: S311  (random нужен для разброса нагрузки, а не для криптографии)
import itertools
import json
import os
import random
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

from locust import HttpUser, between, events, task

API_URL = os.environ.get("API_URL", "http://api:8000")
STUB_URL = os.environ.get("STUB_URL", "http://payment_stub:8000")
CACHE_MODE = os.environ.get("CACHE_MODE", "warm")
PASSWORD = "load-pass-12345"  # noqa: S105  (тестовый пароль одноразовых пользователей)
POLL_INTERVAL = 0.25
POLL_TIMEOUT = 60.0

# Заранее зарегистрированные покупатели (их готовит scripts/load_matrix.py): регистрация и вход это хеширование Argon2, оно
# специально тяжёлое и в начале замера съело бы заметную часть окна. Без файла каждый пользователь регистрируется сам.
TOKENS_FILE = "/mnt/locust/results/buyers.json"
_tokens: list[str] = json.load(open(TOKENS_FILE)) if os.path.exists(TOKENS_FILE) else []  # noqa: SIM115
_next_token = itertools.count()

SEARCH_TERMS = ("0445110", "om651", "0445120", "bosch")
MANUFACTURERS = ("bosch", "denso", "delphi", "siemens")


def _record_e2e(name: str, started: float, exception: Exception | None = None) -> None:
    events.request.fire(
        request_type="E2E",
        name=name,
        response_time=(time.monotonic() - started) * 1000,
        response_length=0,
        exception=exception,
        context={},
    )


class CatalogUser(HttpUser):
    host = API_URL
    wait_time = between(0.05, 0.3)  # короткие паузы: пользователи загружают систему, а не ждут

    def on_start(self) -> None:
        listing = self.client.get(
            "/catalog/injectors", params={"limit": 50}, name="[setup] список"
        ).json()["items"]
        self.ids = [item["id"] for item in listing]

    def _maybe_cold(self) -> None:
        if CACHE_MODE == "cold":
            self.client.post("/debug/cache/reset", name="[cold] сброс кэша")

    @task(4)
    def list_page(self) -> None:
        self._maybe_cold()
        self.client.get("/catalog/injectors", params={"limit": 20}, name="GET список")

    @task(3)
    def filtered(self) -> None:
        self._maybe_cold()
        self.client.get(
            "/catalog/injectors",
            params={"manufacturer": random.choice(MANUFACTURERS), "limit": 20},
            name="GET фильтр",
        )

    @task(3)
    def search(self) -> None:
        self._maybe_cold()
        self.client.get(
            "/catalog/injectors", params={"q": random.choice(SEARCH_TERMS)}, name="GET поиск"
        )

    @task(5)
    def card(self) -> None:
        self._maybe_cold()
        self.client.get(f"/catalog/injectors/{random.choice(self.ids)}", name="GET карточка")


class BuyerUser(HttpUser):
    host = API_URL
    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        if _tokens:
            token = _tokens[next(_next_token) % len(_tokens)]
        else:
            email = f"load-{uuid.uuid4().hex[:12]}@example.com"
            self.client.post(
                "/auth/register",
                json={"email": email, "password": PASSWORD},
                name="[setup] register",
            )
            token = self.client.post(
                "/auth/login", json={"email": email, "password": PASSWORD}, name="[setup] login"
            ).json()["access_token"]
        self.auth = {"Authorization": f"Bearer {token}"}
        listing = self.client.get(
            "/catalog/injectors", params={"limit": 50}, name="[setup] список"
        ).json()["items"]
        self.items = [item["id"] for item in listing if item["stock"] >= 1]

    def _wait(self, path: str, name: str, ready: Any) -> dict[str, Any] | None:
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            data: dict[str, Any] = self.client.get(path, headers=self.auth, name=name).json()
            if ready(data):
                return data
            time.sleep(POLL_INTERVAL)
        return None

    @task
    def buy(self) -> None:
        item_id = random.choice(self.items)
        self.client.put(
            f"/cart/items/{item_id}", json={"quantity": 1}, headers=self.auth, name="PUT корзина"
        )
        with self.client.post(
            "/orders",
            headers={**self.auth, "Idempotency-Key": str(uuid.uuid4())},
            name="POST /orders",
            catch_response=True,
        ) as response:
            if (
                response.status_code == 409
            ):  # товар разобрали: нормальный исход при нагрузке, не ошибка системы
                response.success()
                return
            if response.status_code != 201:
                response.failure(f"статус {response.status_code}")
                return
            order_id = response.json()["id"]

        started = time.monotonic()
        payment = self._wait(
            f"/orders/{order_id}/payment",
            "[poll] платёж",
            lambda p: p.get("status") == "PENDING" and bool(p.get("pay_url")),
        )
        if payment is None:
            _record_e2e("E2E checkout -> PAID", started, TimeoutError("платёж не создан"))
            return
        # pay_url построен для браузера (localhost:8002), внутри сети берём только путь страницы оплаты.
        pay_path = urlsplit(payment["pay_url"]).path
        self.client.post(f"{STUB_URL}{pay_path}/confirm", name="POST оплата (заглушка)")

        paid = self._wait(
            f"/orders/{order_id}", "[poll] заказ", lambda o: o.get("status") == "PAID"
        )
        _record_e2e(
            "E2E checkout -> PAID",
            started,
            None if paid else TimeoutError("заказ не стал PAID"),
        )
