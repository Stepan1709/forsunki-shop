"""Проверка подписи webhook и вызова провайдера (StubPaymentGateway) без сети."""

import hashlib
import hmac
import json
import uuid

import httpx
import pytest

from app.modules.payments.gateway import (
    GatewayPermanentError,
    GatewayTemporaryError,
    InvalidSignatureError,
    MalformedEventError,
    PaymentRequest,
    ProviderEventKind,
)
from app.modules.payments.stub_gateway import StubPaymentGateway
from tests.helpers import WEBHOOK_SECRET

# Тот же вектор проверяется в тестах заглушки (services/payment_stub/tests/test_stub.py): стороны подписывают одинаково.
VECTOR_BODY = b'{"event_id":"e1"}'
VECTOR_TIMESTAMP = 1700000000
VECTOR_SIGNATURE = "2b58d0717977dc093379da0425d7eafe34684c59bcdd466292a99b1f8b9d6c2c"


def gateway(handler: object = None, *, tolerance: int = 300) -> StubPaymentGateway:
    transport = httpx.MockTransport(handler) if handler else None  # type: ignore[arg-type]
    return StubPaymentGateway(
        httpx.AsyncClient(transport=transport),
        base_url="http://stub:8000",
        api_key="secret-key",
        webhook_secret=WEBHOOK_SECRET,
        tolerance_seconds=tolerance,
    )


def signed(body: bytes, timestamp: int, secret: str = WEBHOOK_SECRET) -> dict[str, str]:
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return {"x-stub-signature": f"t={timestamp},v1={digest}"}


def event_body(**overrides: object) -> bytes:
    event = {
        "event_id": "evt-1",
        "type": "payment.succeeded",
        "payment_id": "pay-1",
        "amount_kopecks": 4_500_000,
        "currency": "RUB",
        **overrides,
    }
    return json.dumps(event).encode()


# --------------------------------------------------------------------------------------- подпись
def test_signature_matches_shared_test_vector() -> None:
    headers = {"x-stub-signature": f"t={VECTOR_TIMESTAMP},v1={VECTOR_SIGNATURE}"}

    # Тело вектора не годится как событие, но подпись проходит проверку: ошибка только в формате.
    with pytest.raises(MalformedEventError):
        gateway().verify_webhook(VECTOR_BODY, headers, now=VECTOR_TIMESTAMP)


def test_valid_event_is_parsed() -> None:
    body = event_body()

    event = gateway().verify_webhook(body, signed(body, 1000), now=1010)

    assert event.event_id == "evt-1"
    assert event.kind == ProviderEventKind.SUCCEEDED
    assert event.provider_payment_id == "pay-1"
    assert (event.amount_kopecks, event.currency) == (4_500_000, "RUB")


def test_failed_event_kind() -> None:
    body = event_body(type="payment.failed")

    assert (
        gateway().verify_webhook(body, signed(body, 1000), now=1000).kind
        == ProviderEventKind.FAILED
    )


def test_wrong_secret_is_rejected() -> None:
    body = event_body()

    with pytest.raises(InvalidSignatureError):
        gateway().verify_webhook(body, signed(body, 1000, secret="другой-секрет"), now=1000)


def test_tampered_body_is_rejected() -> None:
    body = event_body(amount_kopecks=100)
    headers = signed(body, 1000)
    tampered = event_body(amount_kopecks=999_999_999)  # подпись от другого тела

    with pytest.raises(InvalidSignatureError):
        gateway().verify_webhook(tampered, headers, now=1000)


@pytest.mark.parametrize("age", [301, 3600, -301])
def test_old_and_future_timestamps_are_rejected(age: int) -> None:
    """Перехваченное событие нельзя подать позже (и «из будущего» тоже)."""
    body = event_body()

    with pytest.raises(InvalidSignatureError):
        gateway().verify_webhook(body, signed(body, 1000), now=1000 + age)


@pytest.mark.parametrize("header", [None, "", "garbage", "t=abc,v1=ff", "v1=ff", "t=1000"])
def test_missing_or_malformed_signature_header_is_rejected(header: str | None) -> None:
    headers = {} if header is None else {"x-stub-signature": header}

    with pytest.raises(InvalidSignatureError):
        gateway().verify_webhook(event_body(), headers, now=1000)


@pytest.mark.parametrize(
    "body",
    [b"not json", b'{"event_id": "x"}', event_body(type="payment.unknown"), b"[1, 2]"],
)
def test_signed_but_malformed_events_are_rejected(body: bytes) -> None:
    with pytest.raises(MalformedEventError):
        gateway().verify_webhook(body, signed(body, 1000), now=1000)


# --------------------------------------------------------------------------------------- вызов провайдера
def request() -> PaymentRequest:
    return PaymentRequest(
        order_id=uuid.UUID(int=7),
        attempt=1,
        amount_kopecks=100_000,
        currency="RUB",
        description="Заказ",
        return_url="http://shop/orders/7",
        correlation_id="cid-9",
    )


def test_idempotency_key_is_order_and_attempt() -> None:
    assert request().idempotency_key == f"{uuid.UUID(int=7)}:1"


async def test_create_payment_sends_authorized_idempotent_request() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(201, json={"payment_id": "pay-1", "pay_url": "http://pay/1"})

    result = await gateway(handler).create_payment(request())

    assert (result.provider_payment_id, result.pay_url) == ("pay-1", "http://pay/1")
    sent = seen[0]
    assert sent.headers["authorization"] == "Bearer secret-key"
    assert sent.headers["idempotency-key"] == request().idempotency_key
    assert sent.headers["x-request-id"] == "cid-9"
    assert json.loads(sent.content)["amount_kopecks"] == 100_000


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_provider_server_errors_are_temporary(status: int) -> None:
    with pytest.raises(GatewayTemporaryError):
        await gateway(lambda r: httpx.Response(status)).create_payment(request())


@pytest.mark.parametrize("status", [400, 401, 404, 409, 422])
async def test_provider_client_errors_are_permanent(status: int) -> None:
    with pytest.raises(GatewayPermanentError):
        await gateway(lambda r: httpx.Response(status)).create_payment(request())


async def test_network_failure_is_temporary() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("нет связи")

    with pytest.raises(GatewayTemporaryError):
        await gateway(handler).create_payment(request())
