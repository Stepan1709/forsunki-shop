"""Трассировка: контекст запроса переживает outbox и RabbitMQ, повторы остаются в той же трассе, trace_id попадает в логи."""

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select

from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.messaging.consumer import MessageContext
from app.core.messaging.errors import TemporaryError
from app.core.outbox import OutboxEventSink, OutboxMessage
from app.core.tracing import current_trace_ids, current_traceparent, setup_tracing
from app.worker.relay import OutboxRelay
from tests.integration.test_messaging import Harness, make_spec, ok, wait_until


@pytest.fixture(scope="module")
def exporter() -> Iterator[InMemorySpanExporter]:
    """Настоящий SDK с экспортом в память. Глобальный провайдер задаётся один раз на процесс."""
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    trace.set_tracer_provider(provider)
    yield memory
    memory.clear()


@pytest.fixture(autouse=True)
def _clean(exporter: InMemorySpanExporter) -> None:
    exporter.clear()


def hex_ids(span: Any) -> tuple[str, str]:
    ctx = span.get_span_context()
    return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")


def finished(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name == name]


async def write_event(app: FastAPI, routing_key: str) -> None:
    async with app.state.resources.session_factory() as session:
        await OutboxEventSink(session).publish(routing_key, {"n": 1})
        await session.commit()


# ------------------------------------------------------------------------------------ outbox
async def test_outbox_row_stores_traceparent_of_current_span(app: FastAPI) -> None:
    with trace.get_tracer("test").start_as_current_span("root") as root:
        trace_id, span_id = hex_ids(root)
        await write_event(app, "test.stored")
    async with app.state.resources.session_factory() as session:
        row = (await session.execute(select(OutboxMessage))).scalar_one()
    # Формат W3C: версия-trace_id-span_id-флаги (флаги зависят от версии SDK, поэтому их не сверяем).
    assert row.traceparent is not None
    version, row_trace, row_span, _flags = row.traceparent.split("-")
    assert (version, row_trace, row_span) == ("00", trace_id, span_id)


async def test_outbox_row_has_no_traceparent_without_active_span(app: FastAPI) -> None:
    assert current_traceparent() is None
    await write_event(app, "test.nospan")
    async with app.state.resources.session_factory() as session:
        row = (await session.execute(select(OutboxMessage))).scalar_one()
    assert row.traceparent is None


# ------------------------------------------------------------------------------------ relay и consumer
async def test_one_trace_goes_through_outbox_broker_and_consumer(
    app: FastAPI, harness: Harness, publisher: Any, exporter: InMemorySpanExporter
) -> None:
    spec = make_spec()
    relay = OutboxRelay(
        app.state.resources.session_factory, publisher, batch_size=10, poll_interval=0.1
    )
    async with harness.running(spec, ok):
        with trace.get_tracer("test").start_as_current_span("root") as root:
            trace_id, root_span_id = hex_ids(root)
            await write_event(app, spec.routing_keys[0])
        assert await relay.run_once() == 1
        await wait_until(lambda: bool(finished(exporter, f"consume {spec.name}")))

    (publish,) = finished(exporter, f"publish {spec.routing_keys[0]}")
    (consume,) = finished(exporter, f"consume {spec.name}")
    # Все три спана в одной трассе, и цепочка родителей не прервана: root -> publish -> consume.
    assert format(publish.context.trace_id, "032x") == trace_id
    assert format(consume.context.trace_id, "032x") == trace_id
    assert publish.parent is not None
    assert format(publish.parent.span_id, "016x") == root_span_id
    assert consume.parent is not None
    assert consume.parent.span_id == publish.context.span_id
    assert consume.attributes is not None
    assert consume.attributes["messaging.system"] == "rabbitmq"


async def test_retries_stay_in_the_same_trace(
    app: FastAPI, harness: Harness, publisher: Any, exporter: InMemorySpanExporter
) -> None:
    spec = make_spec(delays=(0.2, 0.2))
    attempts = 0

    async def flaky(_ctx: MessageContext) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TemporaryError("сбой")

    relay = OutboxRelay(
        app.state.resources.session_factory, publisher, batch_size=10, poll_interval=0.1
    )
    async with harness.running(spec, flaky):
        with trace.get_tracer("test").start_as_current_span("root") as root:
            trace_id, _ = hex_ids(root)
            await write_event(app, spec.routing_keys[0])
        await relay.run_once()
        await wait_until(lambda: len(finished(exporter, f"consume {spec.name}")) >= 2)

    consumes = finished(exporter, f"consume {spec.name}")
    assert {format(s.context.trace_id, "032x") for s in consumes} == {trace_id}
    assert attempts == 2


async def test_consumer_without_traceparent_starts_its_own_trace(
    harness: Harness, publisher: Any, exporter: InMemorySpanExporter
) -> None:
    spec = make_spec()
    async with harness.running(spec, ok):
        await publisher.publish_event(
            spec.routing_keys[0],
            {"n": 1},
            message_id=__import__("uuid").uuid4(),
            correlation_id=None,
        )
        await wait_until(lambda: bool(finished(exporter, f"consume {spec.name}")))
    (consume,) = finished(exporter, f"consume {spec.name}")
    assert consume.parent is None  # старое сообщение без контекста не ломает обработку


# ------------------------------------------------------------------------------------ логи
def test_trace_ids_helper() -> None:
    assert current_trace_ids() is None
    with trace.get_tracer("test").start_as_current_span("x") as current:
        assert current_trace_ids() == hex_ids(current)


def test_logs_contain_trace_id_next_to_correlation_id(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)
    log = structlog.get_logger("trace-test")
    with trace.get_tracer("test").start_as_current_span("x") as current:
        trace_id, span_id = hex_ids(current)
        structlog.contextvars.bind_contextvars(correlation_id="req-123")
        log.info("inside_span")
        structlog.contextvars.clear_contextvars()
    log.info("outside_span")
    lines = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    inside = next(entry for entry in lines if entry["event"] == "inside_span")
    outside = next(entry for entry in lines if entry["event"] == "outside_span")
    assert inside["trace_id"] == trace_id
    assert inside["span_id"] == span_id
    assert inside["correlation_id"] == "req-123"
    assert "trace_id" not in outside


def test_tracing_is_off_without_endpoint(settings: Settings) -> None:
    assert settings.otel_endpoint is None
    assert setup_tracing(settings) is False


async def test_event_published_during_http_request_belongs_to_the_request_trace(
    app: FastAPI, client: Any, exporter: InMemorySpanExporter
) -> None:
    """Событие, созданное в обработчике запроса, получает контекст серверного спана этого запроса."""
    await client.post(
        "/auth/register", json={"email": "t@example.com", "password": "correct-horse-1"}
    )
    await asyncio.sleep(0.05)
    (request_span,) = finished(exporter, "POST /auth/register")
    async with app.state.resources.session_factory() as session:
        row = (
            await session.execute(
                select(OutboxMessage).where(OutboxMessage.routing_key == "user.registered")
            )
        ).scalar_one()
    assert row.traceparent is not None
    assert row.traceparent.split("-")[1] == format(request_span.context.trace_id, "032x")
