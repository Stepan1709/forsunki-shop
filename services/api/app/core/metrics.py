"""Метрики Prometheus: HTTP, кэш, обработка сообщений, outbox и заказы.

Метрики это числа, которые Prometheus забирает с `/metrics` раз в несколько секунд и хранит как временные ряды. В отличие от логов они
дёшевы при любом трафике и отвечают на вопросы «сколько» и «как быстро»: запросов в секунду, доля ошибок, задержка p95, длина очередей.

Важное правило: у метрик не должно быть меток с неограниченным набором значений (id заказа, сырой путь, email). Каждая комбинация меток
это отдельный временной ряд, и их бесконтрольный рост убивает Prometheus. Поэтому в HTTP-метрики идёт шаблон маршрута
(`/orders/{order_id}`), а не реальный адрес.
"""

import time

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Границы корзин гистограммы времени ответа (секунды): от 5 мс до 10 с.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter("http_requests_total", "HTTP-запросы", ["method", "route", "status"])
HTTP_DURATION = Histogram(
    "http_request_duration_seconds",
    "Время обработки HTTP-запроса",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)
HTTP_IN_PROGRESS = Gauge("http_requests_in_progress", "HTTP-запросы в обработке")

CACHE_REQUESTS = Counter(
    "cache_requests_total",
    "Обращения к кэшу каталога: hit (из кэша), miss (загружено из БД), bypass (кэш недоступен)",
    ["result"],
)

RETENTION_DELETED = Counter(
    "retention_deleted_rows_total",
    "Строки, удалённые чисткой старых служебных данных",
    ["table"],
)

CONSUMER_MESSAGES = Counter(
    "consumer_messages_total",
    "Сообщения, обработанные потребителями: processed, duplicate, retried, dead_lettered",
    ["queue", "outcome"],
)
CONSUMER_DURATION = Histogram(
    "consumer_message_duration_seconds",
    "Время обработки одного сообщения",
    ["queue"],
    buckets=_LATENCY_BUCKETS,
)

OUTBOX_PENDING = Gauge("outbox_pending_events", "События в outbox, ещё не отправленные в брокер")
# Насколько самое «просроченное» событие ждёт отправки сверх назначенного времени. Здоровый relay держит значение близким
# к нулю; рост означает, что relay остановился или не успевает. События без подписчика (ждут повтора) сюда не попадают.
OUTBOX_OVERDUE = Gauge(
    "outbox_oldest_overdue_seconds",
    "Просрочка самого старого события outbox, которое пора отправить",
)
OUTBOX_WAITING = Gauge(
    "outbox_unroutable_events",
    "События outbox, для которых нет подписчика (ждут повтора публикации)",
)
ORDERS_BY_STATUS = Gauge("orders_by_status", "Количество заказов по статусам", ["status"])

# Маршрут, которого нет в приложении (сканеры, опечатки): одна метка вместо тысяч разных адресов.
_UNMATCHED = "unmatched"
_SKIPPED_ROUTES = frozenset({"/metrics"})


class MetricsMiddleware:
    """Считает запросы и время ответа. Чистый ASGI-middleware: не теряет contextvars и не буферизует ответ."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in _SKIPPED_ROUTES:
            await self.app(scope, receive, send)
            return

        status = 500  # если приложение упало до ответа, считаем это серверной ошибкой
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
            # Шаблон маршрута известен только после того, как роутер сопоставил запрос.
            route = scope.get("route")
            template = getattr(route, "path", None) or _UNMATCHED
            method = scope["method"]
            HTTP_REQUESTS.labels(method, template, str(status)).inc()
            HTTP_DURATION.labels(method, template).observe(time.perf_counter() - started)


async def metrics_endpoint(_request: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
