"""Метрики Prometheus: HTTP по шаблону маршрута, кэш, исходы consumer'а, заказы и outbox, сервер метрик воркера."""

import asyncio
import socket
import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from prometheus_client import REGISTRY

from app.core.config import Settings
from app.core.messaging.consumer import MessageContext
from app.core.messaging.errors import PermanentError, TemporaryError
from app.worker.runner import Worker
from app.worker.stats import StatsCollector
from tests.helpers import make_settings
from tests.integration.conftest import create_user
from tests.integration.test_messaging import Harness, make_spec, ok, send, wait_until
from tests.integration.test_payments import make_order


def sample(name: str, **labels: str) -> float:
    return float(REGISTRY.get_sample_value(name, labels) or 0.0)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ------------------------------------------------------------------------------------ HTTP
async def test_metrics_endpoint_is_prometheus_text(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in response.text


async def test_http_metrics_use_route_template_not_raw_path(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    labels = {"method": "GET", "route": "/catalog/injectors/{injector_id}", "status": "200"}
    before = sample("http_requests_total", **labels)
    for injector_id in (catalog["bosch_new"], catalog["denso_new"]):
        assert (await client.get(f"/catalog/injectors/{injector_id}")).status_code == 200
    assert (
        sample("http_requests_total", **labels) - before == 2
    )  # два разных id, один временной ряд
    text = (await client.get("/metrics")).text
    assert f'route="/catalog/injectors/{catalog["bosch_new"]}"' not in text


async def test_unknown_paths_share_one_label(client: httpx.AsyncClient) -> None:
    labels = {"method": "GET", "route": "unmatched", "status": "404"}
    before = sample("http_requests_total", **labels)
    for path in ("/wp-admin", "/.env", f"/random-{uuid.uuid4().hex}"):
        assert (await client.get(path)).status_code == 404
    assert sample("http_requests_total", **labels) - before == 3
    assert (
        "/wp-admin" not in (await client.get("/metrics")).text
    )  # сканеры не раздувают число рядов


async def test_http_duration_histogram_and_errors_are_counted(
    client: httpx.AsyncClient, customer_headers: dict[str, str]
) -> None:
    before = sample("http_request_duration_seconds_count", method="GET", route="/users/me")
    await client.get("/users/me", headers=customer_headers)
    assert (
        sample("http_request_duration_seconds_count", method="GET", route="/users/me") - before == 1
    )
    unauthorized = {"method": "GET", "route": "/users/me", "status": "401"}
    before_401 = sample("http_requests_total", **unauthorized)
    await client.get("/users/me")
    assert sample("http_requests_total", **unauthorized) - before_401 == 1


async def test_metrics_endpoint_does_not_count_itself(client: httpx.AsyncClient) -> None:
    labels = {"method": "GET", "route": "/metrics", "status": "200"}
    before = sample("http_requests_total", **labels)
    await client.get("/metrics")
    await client.get("/metrics")
    assert sample("http_requests_total", **labels) == before


# ------------------------------------------------------------------------------------ кэш
async def test_cache_metrics_count_miss_then_hit(
    client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    hits, misses = (
        sample("cache_requests_total", result="hit"),
        sample("cache_requests_total", result="miss"),
    )
    await client.get(f"/catalog/injectors/{catalog['bosch_pd']}")
    await client.get(f"/catalog/injectors/{catalog['bosch_pd']}")
    assert sample("cache_requests_total", result="miss") - misses == 1
    assert sample("cache_requests_total", result="hit") - hits == 1


# ------------------------------------------------------------------------------------ consumer
async def test_consumer_outcomes_are_counted(harness: Harness, publisher: Any) -> None:
    spec = make_spec()
    queue = spec.name

    def counted(outcome: str) -> float:
        return sample("consumer_messages_total", queue=queue, outcome=outcome)

    async with harness.running(spec, ok):
        message_id = uuid.uuid4()
        await send(publisher, spec, {"n": 1}, message_id)
        await send(publisher, spec, {"n": 1}, message_id)  # дубль того же сообщения
        await wait_until(lambda: counted("processed") == 1 and counted("duplicate") == 1)
    assert sample("consumer_message_duration_seconds_count", queue=queue) == 2


async def test_consumer_counts_retry_and_dead_letter(harness: Harness, publisher: Any) -> None:
    spec = make_spec(delays=(0.2, 0.2))
    attempts = 0

    async def flaky(_ctx: MessageContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TemporaryError("сбой")

    async with harness.running(spec, flaky):
        await send(publisher, spec)
        await wait_until(
            lambda: sample("consumer_messages_total", queue=spec.name, outcome="processed") == 1
        )
    assert sample("consumer_messages_total", queue=spec.name, outcome="retried") == 1

    bad_spec = make_spec()

    async def broken(_ctx: MessageContext) -> None:
        raise PermanentError("битое сообщение")

    async with harness.running(bad_spec, broken):
        await send(publisher, bad_spec)
        await wait_until(
            lambda: (
                sample("consumer_messages_total", queue=bad_spec.name, outcome="dead_lettered") == 1
            )
        )


# ------------------------------------------------------------------------------------ заказы и outbox
async def test_stats_collector_exports_orders_and_outbox(
    app: FastAPI, client: httpx.AsyncClient, catalog: dict[str, int]
) -> None:
    _user_id, headers = await create_user(app, "stats@example.com")
    await make_order(app, client, headers, catalog["bosch_new"], 1)
    await asyncio.sleep(0.05)

    await StatsCollector(app.state.resources.session_factory, 15).run_once()
    assert sample("orders_by_status", status="PENDING_PAYMENT") >= 1
    assert sample("orders_by_status", status="PAID") == 0  # статусы без заказов выставлены в 0
    assert sample("outbox_pending_events") >= 2  # order.created и событие отложенного истечения
    assert sample("outbox_oldest_overdue_seconds") >= 0
    assert sample("outbox_unroutable_events") == 0  # у новых событий ещё не было неудачных попыток


# ------------------------------------------------------------------------------------ воркер
async def test_worker_serves_metrics_on_its_own_port(
    app: FastAPI, infra_settings: Settings, secrets_dir: Any
) -> None:
    port = free_port()
    settings = make_settings(
        secrets_dir,
        **{
            k: getattr(infra_settings, k)
            for k in (
                "postgres_host",
                "postgres_port",
                "redis_host",
                "redis_port",
                "rabbitmq_host",
                "rabbitmq_port",
            )
        },
        worker_metrics_port=port,
        worker_heartbeat_path=None,
        metrics_refresh_seconds=0.2,
    )
    worker = Worker(settings, subscriptions=[], delay_queues=[])
    await worker.start()
    try:
        async with httpx.AsyncClient() as http:
            text = ""
            for _ in range(50):
                text = (await http.get(f"http://127.0.0.1:{port}/metrics")).text
                if 'orders_by_status{status="PAID"}' in text:
                    break
                await asyncio.sleep(0.1)
        assert "outbox_pending_events" in text
        assert 'orders_by_status{status="PENDING_PAYMENT"}' in text
    finally:
        await worker.stop()
