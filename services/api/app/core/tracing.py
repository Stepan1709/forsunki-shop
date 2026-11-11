"""Распределённая трассировка (OpenTelemetry): одна трасса проходит через web, api, RabbitMQ, воркер и заглушку провайдера.

Трасса это дерево спанов (отрезков работы): «POST /checkout на сайте» -> «POST /orders в api» -> «INSERT в БД» -> «публикация
события» -> «обработка в воркере» -> «вызов провайдера». Каждый спан знает родителя и время начала и конца, поэтому видно, где
запрос провёл время. Это дополняет `correlation_id`: id связывает записи логов, а трасса показывает структуру и длительность.

Контекст трассы (W3C Trace Context, заголовок `traceparent`) передаётся:
  * между HTTP-сервисами автоматически (инструментация httpx и FastAPI);
  * через RabbitMQ вручную: id хранится в строке outbox рядом с correlation_id, relay кладёт его в заголовок сообщения,
    consumer продолжает ту же трассу. Без этого трасса обрывалась бы на публикации: событие уходит из БД позже и другим процессом.

Трассировка включается, только если задан `OTEL_ENDPOINT` (адрес приёма OTLP, в compose это Jaeger). Без него все вызовы здесь
безопасны и почти бесплатны: API OpenTelemetry без SDK работает как заглушка.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind

from app.core.config import Settings

log = structlog.get_logger("tracing")

TRACEPARENT = "traceparent"
# Эти адреса вызываются каждые несколько секунд пробами и Prometheus: трассы по ним только засоряли бы Jaeger.
_EXCLUDED_URLS = "health/live,health/ready,metrics"

_provider: TracerProvider | None = None


def setup_tracing(settings: Settings) -> bool:
    """Настраивает экспорт трасс. Возвращает True, если трассировка включена. Повторный вызов безопасен."""
    global _provider
    if not settings.otel_endpoint or _provider is not None:
        return _provider is not None
    resource = Resource.create(
        {"service.name": settings.service_name, "deployment.environment": settings.env}
    )
    provider = TracerProvider(resource=resource)
    # BatchSpanProcessor копит спаны и отправляет пачками в фоне: запрос не ждёт сеть. Недоступный Jaeger только
    # порождает предупреждения в логе, на работу сервиса это не влияет.
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces")
        )
    )
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
    # SQL-запросы инструментируются на уровне драйвера asyncpg: так работает с любой версией SQLAlchemy. В спан попадает
    # текст запроса с плейсхолдерами ($1, $2), значения параметров не записываются.
    AsyncPGInstrumentor().instrument()  # type: ignore[no-untyped-call]  # у библиотеки нет аннотаций
    _provider = provider
    log.info("tracing_enabled", endpoint=settings.otel_endpoint)
    return True


def instrument_app(app: FastAPI) -> None:
    if _provider is not None:
        # exclude_spans: служебные спаны отправки и приёма частей ответа несут мало пользы и раздувают трассу.
        FastAPIInstrumentor.instrument_app(
            app, excluded_urls=_EXCLUDED_URLS, exclude_spans=["receive", "send"]
        )


def shutdown_tracing() -> None:
    """Отправляет накопленные спаны перед остановкой процесса."""
    if _provider is not None:
        _provider.shutdown()


def current_traceparent() -> str | None:
    """Заголовок traceparent текущего спана или None, если трассировка выключена или спана нет."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get(TRACEPARENT)


def current_trace_ids() -> tuple[str, str] | None:
    """(trace_id, span_id) текущего спана в виде hex-строк: их добавляют в логи."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def inject_headers(headers: dict[str, Any]) -> dict[str, Any]:
    """Кладёт контекст текущего спана в заголовки сообщения."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {**headers, **carrier}


@contextmanager
def span(
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    parent_traceparent: str | None = None,
    parent_headers: Mapping[str, Any] | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[trace.Span]:
    """Спан, родитель которого берётся из traceparent (строка outbox) или из заголовков сообщения.

    Без родителя спан становится потомком текущего (или корнем новой трассы).
    """
    carrier: dict[str, str] = {}
    if parent_traceparent:
        carrier[TRACEPARENT] = parent_traceparent
    if parent_headers and isinstance(parent_headers.get(TRACEPARENT), str):
        carrier[TRACEPARENT] = str(parent_headers[TRACEPARENT])
    parent = propagate.extract(carrier) if carrier else None
    tracer = trace.get_tracer("forsunki")
    token = otel_context.attach(parent) if parent is not None else None
    try:
        with tracer.start_as_current_span(
            name, kind=kind, attributes=dict(attributes or {})
        ) as current:
            yield current
    finally:
        if token is not None:
            otel_context.detach(token)
