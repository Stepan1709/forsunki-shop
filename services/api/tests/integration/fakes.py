"""Поддельный платёжный провайдер для тестов бизнес-логики.

Реализует тот же интерфейс PaymentGateway, что и StubPaymentGateway, и этим доказывает: сервис платежей, воркер и
заказы не зависят от конкретного провайдера. Подпись здесь не проверяется (у «другого провайдера» она могла бы быть
любой), тело события берётся как есть.
"""

import json
from collections.abc import Mapping

from app.modules.payments.gateway import (
    PaymentRequest,
    ProviderEvent,
    ProviderEventKind,
    ProviderPayment,
)


class FakeGateway:
    name = "fake"

    def __init__(self) -> None:
        self.requests: list[PaymentRequest] = []
        self.failures: list[
            Exception
        ] = []  # исключения, которые нужно бросить при следующих вызовах

    async def create_payment(self, request: PaymentRequest) -> ProviderPayment:
        self.requests.append(request)
        if self.failures:
            raise self.failures.pop(0)
        # Детерминированный ответ по ключу идемпотентности, как у настоящего провайдера.
        return ProviderPayment(
            provider_payment_id=f"fake-{request.idempotency_key}",
            pay_url=f"http://fake-pay/{request.idempotency_key}",
        )

    def verify_webhook(self, body: bytes, headers: Mapping[str, str], now: float) -> ProviderEvent:
        data = json.loads(body)
        return ProviderEvent(
            event_id=data["id"],
            kind=ProviderEventKind(data["kind"]),
            provider_payment_id=data["payment"],
            amount_kopecks=data["amount"],
            currency=data["currency"],
        )
