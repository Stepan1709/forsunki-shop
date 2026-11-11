"""Трассировка заглушки: контекст запроса «создать платёж» сохраняется и используется при отправке webhook'а."""

import sqlite3
from pathlib import Path

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import Settings
from app.storage import Payment, Storage
from app.tracing import current_trace_ids, setup_tracing
from app.webhooks import deliver_once


@pytest.fixture(scope="module")
def exporter() -> InMemorySpanExporter:
    memory = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(memory))
    trace.set_tracer_provider(provider)
    return memory


def fields(key: str = "k1", traceparent: str | None = None) -> dict[str, object]:
    return {
        "idempotency_key": key,
        "order_id": "o-1",
        "amount_kopecks": 100,
        "currency": "RUB",
        "description": "d",
        "return_url": "http://shop/o-1",
        "correlation_id": "cid",
        "traceparent": traceparent,
    }


TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


async def test_traceparent_is_stored_with_payment() -> None:
    storage = Storage(":memory:")
    payment, _ = await storage.create_or_get(fields(traceparent=TRACEPARENT))
    assert payment.traceparent == TRACEPARENT
    again = await storage.get(payment.id)
    assert again is not None
    assert again.traceparent == TRACEPARENT


def test_old_database_without_the_column_is_upgraded(tmp_path: Path) -> None:
    """Том с данными переживает обновление образа: колонка добавляется при открытии базы."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE payments (id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, order_id TEXT NOT NULL,"
        " amount_kopecks INTEGER NOT NULL, currency TEXT NOT NULL, description TEXT NOT NULL, return_url TEXT NOT NULL,"
        " correlation_id TEXT, status TEXT NOT NULL DEFAULT 'pending', event_id TEXT, created_at TEXT NOT NULL,"
        " finalized_at TEXT, webhook_delivered_at TEXT, webhook_attempts INTEGER NOT NULL DEFAULT 0)"
    )
    conn.commit()
    conn.close()
    storage = Storage(path)
    columns = {row[1] for row in storage._conn.execute("PRAGMA table_info(payments)")}
    assert "traceparent" in columns
    storage.close()


async def test_webhook_delivery_continues_the_stored_trace(
    settings: Settings, exporter: InMemorySpanExporter
) -> None:
    exporter.clear()
    storage = Storage(":memory:")
    created, _ = await storage.create_or_get(fields(traceparent=TRACEPARENT))
    payment = await storage.finalize(created.id, "succeeded")
    assert isinstance(payment, Payment)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        assert await deliver_once(settings, client, payment) is True

    (delivery,) = [s for s in exporter.get_finished_spans() if s.name == "deliver webhook"]
    assert format(delivery.context.trace_id, "032x") == "4bf92f3577b34da6a3ce929d0e0e4736"
    assert delivery.parent is not None
    assert format(delivery.parent.span_id, "016x") == "00f067aa0ba902b7"


async def test_delivery_without_stored_context_still_works(
    settings: Settings, exporter: InMemorySpanExporter
) -> None:
    storage = Storage(":memory:")
    created, _ = await storage.create_or_get(fields("k2"))
    payment = await storage.finalize(created.id, "failed")
    assert isinstance(payment, Payment)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200))
    ) as client:
        assert await deliver_once(settings, client, payment) is True


def test_tracing_is_off_without_endpoint_and_ids_are_empty(settings: Settings) -> None:
    assert settings.otel_endpoint is None
    assert setup_tracing(settings) is False
    assert current_trace_ids() is None
