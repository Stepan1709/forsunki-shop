"""Реализация PaymentGateway для заглушки payment_stub (HTTP API + подписанные webhook'и)."""

import hashlib
import hmac
import json
from collections.abc import Mapping

import httpx
import structlog

from app.modules.payments.gateway import (
    GatewayPermanentError,
    GatewayTemporaryError,
    InvalidSignatureError,
    MalformedEventError,
    PaymentRequest,
    ProviderEvent,
    ProviderEventKind,
    ProviderPayment,
)

log = structlog.get_logger("stub_gateway")

SIGNATURE_HEADER = "x-stub-signature"
_EVENT_KINDS = {
    "payment.succeeded": ProviderEventKind.SUCCEEDED,
    "payment.failed": ProviderEventKind.FAILED,
}


class StubPaymentGateway:
    name = "stub"

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str,
        webhook_secret: str,
        tolerance_seconds: int,
        timeout: float = 10.0,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._webhook_secret = webhook_secret
        self._tolerance = tolerance_seconds
        self._timeout = timeout

    async def create_payment(self, request: PaymentRequest) -> ProviderPayment:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Idempotency-Key": request.idempotency_key,
        }
        if request.correlation_id:
            headers["X-Request-ID"] = request.correlation_id
        try:
            response = await self._http.post(
                f"{self._base_url}/payments",
                json={
                    "order_id": str(request.order_id),
                    "amount_kopecks": request.amount_kopecks,
                    "currency": request.currency,
                    "description": request.description,
                    "return_url": request.return_url,
                },
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GatewayTemporaryError(f"провайдер недоступен: {type(exc).__name__}") from exc

        if response.status_code >= 500 or response.status_code == 429:
            raise GatewayTemporaryError(f"провайдер ответил {response.status_code}")
        if response.status_code >= 400:
            # Тело ответа не пишем: в нём могут быть данные платежа.
            raise GatewayPermanentError(f"провайдер отклонил запрос: {response.status_code}")
        data = response.json()
        return ProviderPayment(provider_payment_id=data["payment_id"], pay_url=data["pay_url"])

    def verify_webhook(self, body: bytes, headers: Mapping[str, str], now: float) -> ProviderEvent:
        header = headers.get(SIGNATURE_HEADER)
        if not header:
            raise InvalidSignatureError("нет заголовка подписи")
        try:
            parts = dict(item.split("=", 1) for item in header.split(","))
            timestamp = int(parts["t"])
            received = parts["v1"]
        except (ValueError, KeyError):
            raise InvalidSignatureError("заголовок подписи повреждён") from None

        expected = hmac.new(
            self._webhook_secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
        ).hexdigest()
        # compare_digest: сравнение за постоянное время, подпись нельзя подобрать по скорости ответа.
        if not hmac.compare_digest(expected, received):
            raise InvalidSignatureError("подпись не совпадает")
        # Окно времени: перехваченное когда-то событие нельзя отправить повторно позже.
        if abs(now - timestamp) > self._tolerance:
            raise InvalidSignatureError("подпись просрочена")

        try:
            data = json.loads(body)
            return ProviderEvent(
                event_id=str(data["event_id"]),
                kind=_EVENT_KINDS[data["type"]],
                provider_payment_id=str(data["payment_id"]),
                amount_kopecks=int(data["amount_kopecks"]),
                currency=str(data["currency"]),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise MalformedEventError("неверный формат события") from exc
