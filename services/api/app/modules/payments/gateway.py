"""Интерфейс платёжного провайдера.

Бизнес-код (сервис платежей, воркер, webhook) знает только этот интерфейс и нормализованные структуры.
Настоящая онлайн-касса подключается новой реализацией `PaymentGateway`: создание платежа и проверка подписи
webhook'а у каждого провайдера свои, а всё остальное (заказы, статусы, идемпотентность) остаётся без изменений.
"""

import enum
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.core.messaging.errors import PermanentError, TemporaryError


@dataclass(frozen=True)
class PaymentRequest:
    order_id: uuid.UUID
    attempt: int
    amount_kopecks: int
    currency: str
    description: str
    return_url: str
    correlation_id: str | None

    @property
    def idempotency_key(self) -> str:
        """Ключ идемпотентности для провайдера: заказ + номер попытки.

        Повторный запрос с тем же ключом (воркер упал после вызова провайдера, сообщение пришло снова) вернёт тот
        же платёж, а не создаст второй.
        """
        return f"{self.order_id}:{self.attempt}"


@dataclass(frozen=True)
class ProviderPayment:
    provider_payment_id: str
    pay_url: str


class ProviderEventKind(enum.StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderEvent:
    """Событие от провайдера в едином виде, независимом от его формата."""

    event_id: str
    kind: ProviderEventKind
    provider_payment_id: str
    amount_kopecks: int
    currency: str


class InvalidSignatureError(Exception):
    """Подпись webhook'а неверна, отсутствует или слишком стара."""


class MalformedEventError(Exception):
    """Подпись верна, но содержимое события не соответствует формату."""


class GatewayTemporaryError(TemporaryError):
    """Провайдер недоступен или ответил ошибкой сервера: попытку нужно повторить."""


class GatewayPermanentError(PermanentError):
    """Провайдер отклонил запрос (неверные данные, ключ): повтор не поможет."""


class PaymentGateway(Protocol):
    name: str

    async def create_payment(self, request: PaymentRequest) -> ProviderPayment: ...

    def verify_webhook(self, body: bytes, headers: Mapping[str, str], now: float) -> ProviderEvent:
        """Проверяет подпись и разбирает событие. Подпись считается по СЫРЫМ байтам тела, поэтому тело
        нельзя заранее разбирать и пересобирать."""
        ...
