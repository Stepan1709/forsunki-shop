"""Метрики Prometheus сайта: запросы и время ответа (те же имена, что у api, чтобы один дашборд показывал оба сервиса).

В метки идёт шаблон маршрута (`/orders/{order_id}`), а не реальный адрес: иначе каждый заказ создавал бы новый временной ряд.
"""

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter("http_requests_total", "HTTP-запросы", ["method", "route", "status"])
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "Время обработки HTTP-запроса",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)
HTTP_IN_PROGRESS = Gauge("http_requests_in_progress", "HTTP-запросы в обработке")

_UNMATCHED = (
    "unmatched"  # адреса, которых нет на сайте (сканеры, опечатки): одна метка вместо тысяч
)
_SKIPPED = ("/metrics", "/static/")


class MetricsMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(_SKIPPED):
            await self.app(scope, receive, send)
            return

        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        HTTP_IN_PROGRESS.inc()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            HTTP_IN_PROGRESS.dec()
            template = getattr(scope.get("route"), "path", None) or _UNMATCHED
            method = scope["method"]
            HTTP_REQUESTS.labels(method, template, str(status)).inc()
            HTTP_DURATION.labels(method, template).observe(time.perf_counter() - started)


async def metrics_endpoint(_request: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
