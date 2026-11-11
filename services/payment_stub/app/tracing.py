"""Трассировка заглушки (OpenTelemetry). Включается, только если задан OTEL_ENDPOINT.

Заглушка играет роль внешнего провайдера. Настоящий провайдер не продолжил бы нашу трассу, но для наглядности заглушка запоминает
контекст трассы запроса «создать платёж» и использует его, когда позже шлёт webhook: тогда в Jaeger видна вся цепочка
«заказ -> платёж у провайдера -> webhook -> заказ оплачен» в одной трассе.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import structlog
from fastapi import FastAPI
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.config import Settings

log = structlog.get_logger("tracing")

TRACEPARENT = "traceparent"
_EXCLUDED_URLS = "health/live"
_provider: TracerProvider | None = None


def setup_tracing(settings: Settings) -> bool:
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


def current_traceparent() -> str | None:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get(TRACEPARENT)


def current_trace_ids() -> tuple[str, str] | None:
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return None
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


@contextmanager
def span(name: str, *, parent_traceparent: str | None = None) -> Iterator[trace.Span]:
    """Спан, родитель которого берётся из сохранённого traceparent (или текущий спан, если его нет)."""
    parent = propagate.extract({TRACEPARENT: parent_traceparent}) if parent_traceparent else None
    token = otel_context.attach(parent) if parent is not None else None
    try:
        with trace.get_tracer("forsunki-payment-stub").start_as_current_span(name) as current:
            yield current
    finally:
        if token is not None:
            otel_context.detach(token)
