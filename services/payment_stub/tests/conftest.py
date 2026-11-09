import json
from collections.abc import AsyncIterator, Callable

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr

from app.config import Settings
from app.main import create_app

API_KEY = "k" * 32
WEBHOOK_SECRET = "whsec_test_secret_value_0123456789"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


class WebhookSink:
    """Подменяет магазин: запоминает webhook'и и отвечает заданными кодами."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.statuses: list[int] = []  # очередь ответов; когда пуста, отвечаем 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.statuses.pop(0) if self.statuses else 200, json={"ok": True})

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def sink() -> WebhookSink:
    return WebhookSink()


@pytest.fixture
def settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_path=":memory:",
        payment_api_key=SecretStr(API_KEY),
        payment_webhook_secret=SecretStr(WEBHOOK_SECRET),
        webhook_retry_base_seconds=0.01,
        redeliver_interval_seconds=3600,
        _secrets_dir="/nonexistent",
    )


@pytest_asyncio.fixture
async def app(settings: Settings, sink: WebhookSink) -> AsyncIterator[FastAPI]:
    shop = httpx.AsyncClient(transport=httpx.MockTransport(sink.handler))
    application = create_app(settings, client=shop)
    async with application.router.lifespan_context(application):
        yield application
    await shop.aclose()


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://stub"
    ) as c:
        yield c


@pytest.fixture
def create_payment(client: httpx.AsyncClient) -> Callable[..., object]:
    async def create(
        key: str = "order-1:1", amount: int = 4_500_000, **extra: object
    ) -> httpx.Response:
        body = {
            "order_id": "11111111-2222-3333-4444-555555555555",
            "amount_kopecks": amount,
            "currency": "RUB",
            "return_url": "http://shop/orders/1",
            **extra,
        }
        return await client.post(
            "/payments",
            json=body,
            headers={**AUTH, "Idempotency-Key": key, "X-Request-ID": "cid-1"},
        )

    return create
