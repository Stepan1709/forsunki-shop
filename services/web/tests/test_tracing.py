"""Трассировка сайта: выключена без OTEL_ENDPOINT, trace_id попадает в логи рядом с correlation_id."""

import json

import pytest
import structlog
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from app.config import Settings
from app.logging import configure_logging
from app.tracing import current_trace_ids, setup_tracing


def test_tracing_is_off_without_endpoint(settings: Settings) -> None:
    assert settings.otel_endpoint is None
    assert setup_tracing(settings) is False
    assert current_trace_ids() is None


def test_logs_contain_trace_id_inside_span(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    trace.set_tracer_provider(TracerProvider())
    configure_logging(settings)
    log = structlog.get_logger("trace-test")
    with trace.get_tracer("test").start_as_current_span("x") as span:
        ctx = span.get_span_context()
        structlog.contextvars.bind_contextvars(correlation_id="req-1")
        log.info("inside")
        structlog.contextvars.clear_contextvars()
    log.info("outside")
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    inside = next(entry for entry in lines if entry["event"] == "inside")
    outside = next(entry for entry in lines if entry["event"] == "outside")
    assert inside["trace_id"] == format(ctx.trace_id, "032x")
    assert inside["span_id"] == format(ctx.span_id, "016x")
    assert inside["correlation_id"] == "req-1"
    assert "trace_id" not in outside
