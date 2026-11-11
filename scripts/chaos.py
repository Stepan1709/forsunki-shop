"""Хаос-сценарии на запущенном стеке (`make up-full` или `make up`): ломаем компоненты и проверяем инварианты.

Запуск: `make chaos`. Скрипт останавливает, убивает и перезапускает контейнеры dev-стека (docker compose), поэтому его нельзя
запускать на стеке, которым в этот момент кто-то пользуется.

Сценарии:
  1. воркер убит `kill -9` до создания платежа и (отдельным заказом) до отправки письма;
  2. RabbitMQ остановлен на время оформления заказа (outbox должен довезти события после возврата брокера);
  3. Redis остановлен (сайт деградирует, заказы и оплата продолжают работать);
  4. Postgres остановлен на время оплаты (webhook'и провайдера повторяются, пока магазин не ответит).

После КАЖДОГО сценария проверяются одни и те же инварианты:
  * заказ оплачен и ровно один платёж SUCCEEDED;
  * остаток уменьшился ровно на число купленных штук (не больше и не меньше);
  * покупатель получил ровно одно письмо об оплате;
  * новых сообщений в DLQ нет;
  * в outbox не осталось неотправленных событий этого заказа.
"""

# ruff: noqa: S607, S608  (SQL собирается из id заказов, которые создаёт сам скрипт, а не из внешнего ввода)
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
API = "http://localhost:8001"
WEB = "http://localhost:8000"
STUB = "http://localhost:8002"
MAILPIT = "http://localhost:8025"
PASSWORD = "chaos-pass-12345"  # noqa: S105  (тестовый пароль одноразового пользователя)
COMPOSE = [
    "docker",
    "compose",
    "--env-file",
    ".env",
    "-f",
    "deploy/compose.yml",
    "-f",
    "deploy/compose.dev.yml",
]
RESULTS: list[tuple[str, float]] = []


def step(message: str) -> None:
    print(f"  ok  {message}", flush=True)


def info(message: str) -> None:
    print(f"  ..  {message}", flush=True)


def fail(message: str) -> None:
    print(f"FAIL  {message}", flush=True)
    sys.exit(1)


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [*COMPOSE, *args], cwd=ROOT, check=check, capture_output=True, text=True
    )


def wait_for(description: str, check: Any, timeout: float = 90.0, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            value = check()
        except (httpx.HTTPError, subprocess.CalledProcessError, KeyError, ValueError):
            value = None
        if value:
            return value
        time.sleep(interval)
    fail(f"не дождались: {description}")


def health(service: str) -> str:
    container = compose("ps", "-q", service, check=False).stdout.strip()
    if not container:
        return "absent"
    out = subprocess.run(  # noqa: S603
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container,
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return out


def wait_healthy(service: str, timeout: float = 120.0) -> None:
    wait_for(
        f"{service} healthy", lambda: health(service) in ("running/healthy", "running/"), timeout
    )


def psql(sql: str) -> str:
    return compose(
        "exec", "-T", "postgres", "psql", "-U", "forsunki", "-d", "forsunki", "-tAc", sql
    ).stdout.strip()


def dlq_total() -> int:
    out = compose(
        "exec", "-T", "rabbitmq", "rabbitmqctl", "list_queues", "name", "messages", "-q"
    ).stdout
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].endswith(".dlq"):
            total += int(parts[1])
    return total


class Buyer:
    """Покупатель с токеном: входит заранее, чтобы сценарий с остановленным Redis не упирался во вход."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        self.email = f"chaos-{uuid.uuid4().hex[:10]}@example.com"
        client.post(
            f"{API}/auth/register", json={"email": self.email, "password": PASSWORD}
        ).raise_for_status()
        tokens = client.post(
            f"{API}/auth/login", json={"email": self.email, "password": PASSWORD}
        ).json()
        self.auth = {"Authorization": f"Bearer {tokens['access_token']}"}

    def place_order(self, item_id: int, quantity: int) -> dict[str, Any]:
        self.client.put(
            f"{API}/cart/items/{item_id}", json={"quantity": quantity}, headers=self.auth
        ).raise_for_status()
        response = self.client.post(
            f"{API}/orders", headers={**self.auth, "Idempotency-Key": str(uuid.uuid4())}
        )
        if response.status_code != 201:
            fail(f"заказ не создан: {response.status_code} {response.text}")
        return dict(response.json())

    def order(self, order_id: str) -> dict[str, Any]:
        return dict(self.client.get(f"{API}/orders/{order_id}", headers=self.auth).json())

    def pay_url(self, order_id: str, timeout: float = 90.0) -> str:
        def ready() -> str | None:
            response = self.client.get(f"{API}/orders/{order_id}/payment", headers=self.auth)
            payment = response.json()
            return str(payment["pay_url"]) if payment.get("status") == "PENDING" else None

        return str(wait_for("платёж создан (ссылка на оплату)", ready, timeout))

    def pay(self, pay_url: str) -> None:
        self.client.post(f"{pay_url}/confirm", follow_redirects=True).raise_for_status()


def stock_of(client: httpx.Client, item_id: int) -> int:
    client.post(f"{API}/debug/cache/reset")  # остаток в кэше каталога приблизительный
    return int(client.get(f"{API}/catalog/injectors/{item_id}").json()["stock"])


def paid_mails(client: httpx.Client, email: str) -> int:
    found = client.get(f"{MAILPIT}/api/v1/messages").json()["messages"]
    return len(
        [
            m
            for m in found
            if any(t["Address"] == email for t in m["To"]) and "оплачен" in m["Subject"]
        ]
    )


def check_invariants(
    client: httpx.Client,
    buyer: Buyer,
    order_id: str,
    item_id: int,
    quantity: int,
    stock_before: int,
    dlq_before: int,
) -> None:
    wait_for("заказ PAID", lambda: buyer.order(order_id)["status"] == "PAID", 120)
    step("заказ оплачен (PAID)")

    succeeded = int(
        psql(
            f"SELECT count(*) FROM payments WHERE order_id = '{order_id}' AND status = 'SUCCEEDED'"
        )
    )
    if succeeded != 1:
        fail(f"успешных платежей по заказу: {succeeded}, ожидался 1")
    step("ровно один платёж SUCCEEDED")

    now = stock_of(client, item_id)
    if now != stock_before - quantity:
        fail(f"остаток {now}, ожидался {stock_before - quantity}")
    step(f"остаток уменьшился ровно на {quantity}")

    wait_for("письмо об оплате", lambda: paid_mails(client, buyer.email) >= 1, 120)
    time.sleep(2)  # возможное второе письмо должно успеть прийти
    mails = paid_mails(client, buyer.email)
    if mails != 1:
        fail(f"писем об оплате: {mails}, ожидалось 1")
    step("покупатель получил ровно одно письмо об оплате")

    wait_for(
        "outbox доставлен",
        lambda: (
            int(
                psql(
                    "SELECT count(*) FROM outbox WHERE sent_at IS NULL "
                    f"AND payload->>'order_id' = '{order_id}'"
                )
            )
            == 0
        ),
        120,
    )
    step("в outbox нет неотправленных событий заказа")

    if dlq_total() != dlq_before:
        fail(f"в DLQ появились сообщения: было {dlq_before}, стало {dlq_total()}")
    step("в DLQ ничего не попало")


def pick_item(client: httpx.Client) -> int:
    listing = client.get(f"{API}/catalog/injectors", params={"limit": 50}).json()["items"]
    item = next((i for i in listing if i["stock"] >= 5), None)
    if item is None:
        fail("в каталоге нет товара с остатком >= 5 (выполните make seed)")
    return int(item["id"])


def scenario(name: str) -> Any:
    def decorator(func: Any) -> Any:
        def run(client: httpx.Client, item_id: int) -> None:
            print(f"\nСценарий: {name}", flush=True)
            started = time.monotonic()
            func(client, item_id)
            RESULTS.append((name, time.monotonic() - started))

        run.__name__ = func.__name__
        return run

    return decorator


@scenario("воркер убит до создания платежа и до отправки письма")
def worker_killed(client: httpx.Client, item_id: int) -> None:
    buyer = Buyer(client)
    stock_before, dlq_before = stock_of(client, item_id), dlq_total()

    compose("kill", "-s", "KILL", "worker")
    info("воркер убит (SIGKILL)")
    order = buyer.place_order(item_id, 2)
    time.sleep(3)
    status = buyer.client.get(f"{API}/orders/{order['id']}/payment", headers=buyer.auth).json()
    # Docker может сам перезапустить убитый контейнер (restart: unless-stopped), поэтому это справочная строка.
    info(f"через 3 с после убийства воркера платёж: {status.get('status', 'не создан')}")

    compose("up", "-d", "--no-deps", "worker")
    pay_url = buyer.pay_url(order["id"])
    step("воркер вернулся и создал платёж из сохранённого сообщения")

    compose("kill", "-s", "KILL", "worker")
    info("воркер убит повторно, перед оплатой")
    buyer.pay(pay_url)
    wait_for("PAID без воркера", lambda: buyer.order(order["id"])["status"] == "PAID", 60)
    step("оплата принята api без воркера (webhook обработан)")
    compose("up", "-d", "--no-deps", "worker")
    wait_healthy("worker")

    check_invariants(client, buyer, order["id"], item_id, 2, stock_before, dlq_before)


@scenario("RabbitMQ остановлен на время оформления заказа")
def rabbitmq_down(client: httpx.Client, item_id: int) -> None:
    buyer = Buyer(client)
    stock_before, dlq_before = stock_of(client, item_id), dlq_total()

    compose("stop", "rabbitmq")
    info("RabbitMQ остановлен")
    started = time.monotonic()
    order = buyer.place_order(item_id, 1)
    elapsed = time.monotonic() - started
    if elapsed > 5:
        fail(f"заказ оформлялся {elapsed:.1f} с: api ждёт брокера")
    step(f"заказ оформлен без брокера за {elapsed:.2f} с (событие лежит в outbox)")
    pending = int(
        psql(
            f"SELECT count(*) FROM outbox WHERE sent_at IS NULL AND payload->>'order_id' = '{order['id']}'"
        )
    )
    if pending < 1:
        fail("события заказа нет в outbox")
    step("событие order.created ждёт в outbox")

    compose("start", "rabbitmq")
    wait_healthy("rabbitmq")
    info("RabbitMQ вернулся")
    pay_url = buyer.pay_url(order["id"])
    step("relay и воркер переподключились, платёж создан")
    buyer.pay(pay_url)

    check_invariants(client, buyer, order["id"], item_id, 1, stock_before, dlq_before)


@scenario("Redis остановлен: сайт деградирует, заказы работают")
def redis_down(client: httpx.Client, item_id: int) -> None:
    buyer = Buyer(client)  # токен получен заранее: вход при остановленном Redis не проверяем
    stock_before, dlq_before = stock_of(client, item_id), dlq_total()

    compose("stop", "redis")
    info("Redis остановлен")
    listing = client.get(f"{API}/catalog/injectors", params={"limit": 5})
    if listing.status_code != 200:
        fail(f"каталог недоступен без Redis: {listing.status_code}")
    step("каталог отвечает без кэша (прямо из БД)")

    for path in ("/", "/shop"):
        try:
            status = httpx.get(f"{WEB}{path}", timeout=15).status_code
        except httpx.HTTPError as exc:
            status = type(exc).__name__
        info(f"сайт {path} без Redis: {status}")

    order = buyer.place_order(item_id, 1)
    step("заказ оформлен без Redis")
    pay_url = buyer.pay_url(order["id"])
    buyer.pay(pay_url)
    wait_for("PAID без Redis", lambda: buyer.order(order["id"])["status"] == "PAID", 60)
    step("оплата принята без Redis")

    compose("start", "redis")
    wait_healthy("redis")
    info("Redis вернулся")
    check_invariants(client, buyer, order["id"], item_id, 1, stock_before, dlq_before)


@scenario("Postgres остановлен на время оплаты")
def postgres_down(client: httpx.Client, item_id: int) -> None:
    buyer = Buyer(client)
    stock_before, dlq_before = stock_of(client, item_id), dlq_total()
    order = buyer.place_order(item_id, 1)
    pay_url = buyer.pay_url(order["id"])

    compose("stop", "postgres")
    info("Postgres остановлен")
    ready = client.get(f"{API}/health/ready")
    if ready.status_code == 200:
        fail("/health/ready отвечает 200 без БД")
    step(f"/health/ready сообщает о проблеме ({ready.status_code})")

    started = time.monotonic()
    try:
        response = client.post(
            f"{API}/orders", headers={**buyer.auth, "Idempotency-Key": str(uuid.uuid4())}
        )
        code: int | str = response.status_code
    except httpx.HTTPError as exc:
        code = type(exc).__name__
    elapsed = time.monotonic() - started
    if code != 503 or elapsed > 20:
        fail(f"оформление при остановленной БД: {code} за {elapsed:.1f} с")
    step(f"оформление при остановленной БД быстро отклонено ({code}, {elapsed:.1f} с)")

    buyer.pay(pay_url)
    info("покупатель оплатил, пока БД недоступна: webhook провайдера не принят")
    time.sleep(8)  # несколько неудачных попыток доставки у провайдера

    compose("start", "postgres")
    wait_healthy("postgres")
    wait_for("api снова готов", lambda: client.get(f"{API}/health/ready").status_code == 200, 120)
    info("Postgres вернулся, провайдер повторяет webhook")

    check_invariants(client, buyer, order["id"], item_id, 1, stock_before, dlq_before)


def main() -> None:
    client = httpx.Client(timeout=15)
    for service in ("api", "worker", "postgres", "redis", "rabbitmq", "payment_stub"):
        state = health(service)
        if not state.startswith("running"):
            fail(f"сервис {service} не запущен ({state}): сначала make up")
    item_id = pick_item(client)

    started = time.monotonic()
    for run in (worker_killed, rabbitmq_down, redis_down, postgres_down):
        run(client, item_id)

    print("\nИтоги:")
    for name, seconds in RESULTS:
        print(f"  {seconds:6.1f} с  {name}")
    print(json.dumps({"total_seconds": round(time.monotonic() - started, 1)}))
    print("Все хаос-сценарии пройдены")


if __name__ == "__main__":
    main()
