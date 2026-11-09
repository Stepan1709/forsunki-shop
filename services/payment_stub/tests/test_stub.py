import asyncio
import hashlib
import hmac
import re
from collections.abc import Callable
from typing import Any

import httpx

from app.config import Settings
from app.signing import sign
from app.storage import Storage
from app.webhooks import deliver_with_retries
from tests.conftest import AUTH, WEBHOOK_SECRET, WebhookSink

# Тот же вектор проверяется в тестах api (tests/unit/test_payment_signature.py): алгоритм подписи у сторон общий.
VECTOR_BODY = b'{"event_id":"e1"}'
VECTOR = "t=1700000000,v1=2b58d0717977dc093379da0425d7eafe34684c59bcdd466292a99b1f8b9d6c2c"


def test_signature_matches_shared_test_vector() -> None:
    assert sign(WEBHOOK_SECRET, 1700000000, VECTOR_BODY) == VECTOR


async def test_api_requires_key(client: httpx.AsyncClient) -> None:
    body = {"order_id": "o", "amount_kopecks": 100, "currency": "RUB", "return_url": "x"}

    assert (await client.post("/payments", json=body)).status_code == 401
    wrong = await client.post("/payments", json=body, headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


async def test_create_payment_is_idempotent(create_payment: Callable[..., Any]) -> None:
    first = await create_payment()
    again = await create_payment()

    assert first.status_code == 201
    assert again.status_code == 200
    assert again.json() == first.json()  # тот же платёж, а не второй
    assert first.json()["pay_url"].endswith(f"/pay/{first.json()['payment_id']}")


async def test_same_key_with_different_amount_is_rejected(
    create_payment: Callable[..., Any],
) -> None:
    await create_payment(amount=100_000)

    assert (await create_payment(amount=999_999)).status_code == 409


async def test_idempotency_key_is_required(client: httpx.AsyncClient) -> None:
    body = {"order_id": "o", "amount_kopecks": 100, "currency": "RUB", "return_url": "x"}

    assert (await client.post("/payments", json=body, headers=AUTH)).status_code == 400


async def test_pay_page_shows_amount_and_buttons(
    client: httpx.AsyncClient, create_payment: Callable[..., Any]
) -> None:
    payment_id = (await create_payment(amount=4_500_050)).json()["payment_id"]

    page = await client.get(f"/pay/{payment_id}")

    assert page.status_code == 200
    assert "45 000,50" in page.text
    assert "Оплатить" in page.text
    assert "Отклонить" in page.text


async def test_unknown_payment_page_is_404(client: httpx.AsyncClient) -> None:
    page = await client.get("/pay/does-not-exist")

    assert page.status_code == 404
    assert "Платёж не найден" in page.text


async def test_confirm_sends_one_signed_webhook(
    client: httpx.AsyncClient, create_payment: Callable[..., Any], sink: WebhookSink
) -> None:
    created = (await create_payment()).json()

    redirect = await client.post(f"/pay/{created['payment_id']}/confirm")

    assert redirect.status_code == 303
    assert len(sink.requests) == 1
    request = sink.requests[0]
    event = sink.bodies()[0]
    assert event["type"] == "payment.succeeded"
    assert event["payment_id"] == created["payment_id"]
    assert event["amount_kopecks"] == 4_500_000
    assert event["currency"] == "RUB"
    assert request.headers["x-request-id"] == "cid-1"  # сквозной id запроса дошёл до webhook
    # Подпись можно проверить независимо, зная только секрет.
    timestamp = re.search(r"t=(\d+)", request.headers["x-stub-signature"]).group(1)  # type: ignore[union-attr]
    expected = hmac.new(
        WEBHOOK_SECRET.encode(), f"{timestamp}.".encode() + request.content, hashlib.sha256
    ).hexdigest()
    assert request.headers["x-stub-signature"] == f"t={timestamp},v1={expected}"
    page = await client.get(f"/pay/{created['payment_id']}")
    assert "Оплата прошла" in page.text


async def test_double_click_does_not_send_second_webhook_or_change_result(
    client: httpx.AsyncClient, create_payment: Callable[..., Any], sink: WebhookSink
) -> None:
    payment_id = (await create_payment()).json()["payment_id"]

    await client.post(f"/pay/{payment_id}/confirm")
    await client.post(f"/pay/{payment_id}/confirm")
    await client.post(f"/pay/{payment_id}/decline")  # поздний «отказ» после оплаты

    assert len(sink.requests) == 1
    assert sink.bodies()[0]["type"] == "payment.succeeded"


async def test_decline_sends_failed_event(
    client: httpx.AsyncClient, create_payment: Callable[..., Any], sink: WebhookSink
) -> None:
    payment_id = (await create_payment()).json()["payment_id"]

    await client.post(f"/pay/{payment_id}/decline")

    assert sink.bodies()[0]["type"] == "payment.failed"
    assert "Платёж отклонён" in (await client.get(f"/pay/{payment_id}")).text


async def test_resend_repeats_the_same_event_id(
    client: httpx.AsyncClient, create_payment: Callable[..., Any], sink: WebhookSink
) -> None:
    payment_id = (await create_payment()).json()["payment_id"]
    await client.post(f"/pay/{payment_id}/confirm")

    resend = await client.post(f"/payments/{payment_id}/webhook/resend", headers=AUTH)

    assert resend.json() == {"delivered": True}
    first, second = sink.bodies()
    assert first["event_id"] == second["event_id"]  # дубль: получатель отсечёт его по event_id


async def test_resend_before_completion_is_rejected(
    client: httpx.AsyncClient, create_payment: Callable[..., Any]
) -> None:
    payment_id = (await create_payment()).json()["payment_id"]

    resend = await client.post(f"/payments/{payment_id}/webhook/resend", headers=AUTH)

    assert resend.status_code == 409


async def test_webhook_is_retried_until_shop_accepts(
    app: Any, settings: Settings, sink: WebhookSink
) -> None:
    """Магазин временно недоступен (500, 503): событие доставляется повторно, и не теряется."""
    storage: Storage = app.state.storage
    payment, _ = await storage.create_or_get(
        {
            "idempotency_key": "k1",
            "order_id": "o",
            "amount_kopecks": 100,
            "currency": "RUB",
            "description": "d",
            "return_url": "x",
            "correlation_id": None,
        }
    )
    finalized = await storage.finalize(payment.id, "succeeded")
    assert finalized is not None
    sink.statuses = [500, 503]  # два сбоя, затем 200

    await deliver_with_retries(settings, app.state.http, storage, finalized)

    assert len(sink.requests) == 3
    assert (await storage.get(payment.id)).webhook_delivered_at is not None  # type: ignore[union-attr]
    assert await storage.undelivered(settings.webhook_max_attempts) == []


async def test_concurrent_confirms_finalize_once(
    client: httpx.AsyncClient, create_payment: Callable[..., Any], sink: WebhookSink
) -> None:
    payment_id = (await create_payment()).json()["payment_id"]

    await asyncio.gather(*(client.post(f"/pay/{payment_id}/confirm") for _ in range(6)))

    assert len(sink.requests) == 1
