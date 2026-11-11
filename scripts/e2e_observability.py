"""Проверка наблюдаемости на живом стеке (`make up-full`, затем `make e2e`): метрики в Prometheus и сквозная трасса в Jaeger.

Запускается после сценариев заказа и оплаты, поэтому метрики уже содержат трафик, а в Jaeger лежит трасса оформления заказа.
Если профили monitoring или tracing не запущены (обычный `make up`), соответствующая проверка пропускается.
"""

import sys
import time
import urllib.parse

import httpx

PROMETHEUS = "http://localhost:9090"
JAEGER = "http://localhost:16686"
EXPECTED_JOBS = {"api", "web", "worker", "rabbitmq"}
EXPECTED_SERVICES = {"web", "api", "worker", "payment_stub"}


def step(message: str) -> None:
    print(f"  ok  {message}")


def fail(message: str) -> None:
    print(f"FAIL  {message}")
    sys.exit(1)


def reachable(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code < 500
    except httpx.HTTPError:
        return False


def query(expr: str) -> list[dict[str, object]]:
    response = httpx.get(f"{PROMETHEUS}/api/v1/query", params={"query": expr}, timeout=10)
    result: list[dict[str, object]] = response.json()["data"]["result"]
    return result


def check_metrics() -> None:
    if not reachable(f"{PROMETHEUS}/-/ready"):
        print("  --  Prometheus не запущен (профиль monitoring), проверка метрик пропущена")
        return
    targets = httpx.get(f"{PROMETHEUS}/api/v1/targets", timeout=10).json()["data"]["activeTargets"]
    state = {t["labels"]["job"]: t["health"] for t in targets}
    down = [job for job in EXPECTED_JOBS if state.get(job) != "up"]
    if down:
        fail(f"Prometheus не видит сервисы: {down} (состояние: {state})")
    step("Prometheus собирает метрики api, web, worker и RabbitMQ")

    for job in ("api", "web"):
        if not query(f'sum(http_requests_total{{job="{job}"}}) > 0'):
            fail(f"у {job} нет метрик запросов")
    step("метрики запросов api и web ненулевые")

    if not query('max(orders_by_status{status="PAID"}) > 0'):
        fail("в метриках нет оплаченных заказов (воркер обновляет их раз в 15 секунд)")
    step("метрика заказов по статусам видит оплаченные заказы")
    if not query('sum(consumer_messages_total{outcome="processed"}) > 0'):
        fail("воркер не посчитал обработанные сообщения")
    step("воркер считает обработанные сообщения")
    if not query('count(rabbitmq_detailed_queue_messages_ready{queue=~".*[.]dlq"}) > 0'):
        fail("в метриках RabbitMQ нет очередей DLQ")
    step("метрики RabbitMQ показывают очереди и DLQ")


def find_trace() -> dict[str, object] | None:
    operation = urllib.parse.quote("POST /checkout")
    url = f"{JAEGER}/api/traces?service=web&operation={operation}&limit=10&lookback=15m"
    traces: list[dict[str, object]] = httpx.get(url, timeout=10).json()["data"]
    for trace in traces:
        names = {p["serviceName"] for p in trace["processes"].values()}  # type: ignore[attr-defined]
        if names >= EXPECTED_SERVICES:
            return trace
    return None


def check_trace() -> None:
    if not reachable(f"{JAEGER}/api/services"):
        print("  --  Jaeger не запущен (профиль tracing), проверка трассы пропущена")
        return
    trace = None
    for _ in range(20):  # спаны уходят в Jaeger пачками раз в несколько секунд
        trace = find_trace()
        if trace:
            break
        time.sleep(1.5)
    if trace is None:
        fail(
            f"в Jaeger нет трассы «POST /checkout», охватывающей сервисы {sorted(EXPECTED_SERVICES)}"
        )
        return
    operations = {s["operationName"] for s in trace["spans"]}  # type: ignore[attr-defined]
    for needed in (
        "POST /orders",
        "publish order.created",
        "consume payments.order_created",
        "deliver webhook",
        "POST /payments/webhook",
        "consume notifications.order_paid",
    ):
        if needed not in operations:
            fail(f"в сквозной трассе нет спана «{needed}»")
    step("одна трасса охватывает web, api, RabbitMQ, воркер, провайдера, webhook и письмо")


if __name__ == "__main__":
    check_metrics()
    check_trace()
    print("Все проверки пройдены")
