"""Трассировка сайта (OpenTelemetry). Включается, только если задан OTEL_ENDPOINT.

Сайт начинает трассу (корневой спан «POST /checkout»), а httpx-инструментация сама добавляет заголовок traceparent к каждому
запросу в api: на стороне api спаны становятся потомками спана сайта, и в Jaeger виден один запрос целиком.
"""

import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings

log = structlog.get_logger("tracing")

# Пробы, метрики и статические файлы вызываются постоянно: трассы по ним только засоряли бы Jaeger.
_EXCLUDED_URLS = "health/live,health/ready,metrics,static"
_provider: TracerProvider | None = None


def setup_tracing(settings: Settings) -> bool:
    """Настраивает экспорт трасс. Возвращает True, если трассировка включена. Повторный вызов безопасен."""
    global _provider
    if not settings.otel_endpoint or _provider is not None:
        return _provider is not None
    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": settings.service_name, "deployment.environment": settings.env}
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(endpoint=f"{settings.otel_endpoint.rstrip('/')}/v1/traces")
        )
    )
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    RedisInstrumentor().instrument()
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
    if _provider is not None:
        _provider.shutdown()


def current_trace_ids() -> tuple[str, str] | None:
    """(trace_id, span_id) текущего спана в виде hex-строк: их добавляют в логи рядом с correlation_id."""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
