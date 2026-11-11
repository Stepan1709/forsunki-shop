"""Метрики сайта: запросы считаются по шаблону маршрута, адреса сканеров не раздувают число рядов."""

import httpx
from prometheus_client import REGISTRY


def sample(name: str, **labels: str) -> float:
    return float(REGISTRY.get_sample_value(name, labels) or 0.0)


async def test_metrics_endpoint_serves_prometheus_text(client: httpx.AsyncClient) -> None:
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in response.text


async def test_requests_are_counted_by_route_template(client: httpx.AsyncClient) -> None:
    labels = {"method": "GET", "route": "/shop/{injector_id}", "status": "200"}
    before = sample("http_requests_total", **labels)
    await client.get("/shop/1")
    assert sample("http_requests_total", **labels) - before == 1
    assert 'route="/shop/1"' not in (await client.get("/metrics")).text


async def test_unknown_paths_share_one_label_and_metrics_do_not_count_themselves(
    client: httpx.AsyncClient,
) -> None:
    unmatched = {"method": "GET", "route": "unmatched", "status": "404"}
    before = sample("http_requests_total", **unmatched)
    await client.get("/wp-login.php")
    await client.get("/.git/config")
    assert sample("http_requests_total", **unmatched) - before == 2
    assert "wp-login" not in (await client.get("/metrics")).text

    own = {"method": "GET", "route": "/metrics", "status": "200"}
    before_own = sample("http_requests_total", **own)
    await client.get("/metrics")
    assert sample("http_requests_total", **own) == before_own


async def test_duration_histogram_is_recorded(client: httpx.AsyncClient) -> None:
    before = sample("http_request_duration_seconds_count", method="GET", route="/repair")
    await client.get("/repair")
    assert (
        sample("http_request_duration_seconds_count", method="GET", route="/repair") - before == 1
    )
