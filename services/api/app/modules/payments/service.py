"""Платежи: создание платежа у провайдера и обработка его webhook'ов."""

import contextlib
import enum
import time
import uuid
from collections.abc import Mapping

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, UnauthorizedError
from app.core.events import EventSink
from app.core.messaging.errors import PermanentError, TemporaryError
from app.modules.orders.repository import OrderRepository
from app.modules.orders.service import OrderService
from app.modules.orders.state import InvalidTransitionError, OrderStatus
from app.modules.payments.gateway import (
    InvalidSignatureError,
    MalformedEventError,
    PaymentGateway,
    PaymentRequest,
    ProviderEvent,
    ProviderEventKind,
)
from app.modules.payments.models import Payment, PaymentStatus
from app.modules.payments.repository import PaymentRepository

log = structlog.get_logger("payments")


class WebhookResult(enum.StrEnum):
    PROCESSED = "processed"  # событие применено
    DUPLICATE = "duplicate"  # это событие уже обрабатывалось
    IGNORED = "ignored"  # событие не требует действий (неизвестный платёж, платёж уже завершён)
    REJECTED = "rejected"  # событие не сошлось с платежом, нужен разбор человеком


class PaymentService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        gateway: PaymentGateway,
        events: EventSink,
    ) -> None:
        self._session = session
        self._settings = settings
        self._gateway = gateway
        self._events = events
        self._payments = PaymentRepository(session)
        self._order_repo = OrderRepository(session)
        self._orders = OrderService(session, settings, events)

    # ------------------------------------------------------------------------------------ создание
    async def create_for_order(
        self, order_id: uuid.UUID, correlation_id: str | None
    ) -> Payment | None:
        """Создаёт платёж по событию order.created. Коммит выполняет вызывающий код (consumer).

        Идемпотентно: повторная обработка того же события не создаёт второй платёж. Если вызов провайдера упал,
        вся транзакция откатывается, а повтор отправит провайдеру тот же ключ `order_id:attempt`, и тот вернёт
        тот же платёж.
        """
        order = await self._order_repo.get(order_id)
        if order is None:
            raise PermanentError(f"заказ {order_id} не найден")
        if order.status != OrderStatus.PENDING_PAYMENT:
            # Заказ успели отменить или он истёк, пока событие ждало в очереди: платить уже нечего.
            log.info(
                "payment_skipped_order_closed", order_id=str(order_id), status=order.status.value
            )
            return None

        existing = await self._payments.latest_for_order(order_id)
        if existing is not None and existing.provider_payment_id is not None:
            return existing  # событие пришло повторно, платёж уже создан

        payment = existing or Payment(
            order_id=order_id,
            attempt=1,
            status=PaymentStatus.PENDING,
            provider=self._gateway.name,
            amount_kopecks=order.total_kopecks,
            currency=order.currency,
        )
        if existing is None:
            self._payments.add(payment)
            try:
                await self._session.flush()
            except IntegrityError:
                raise TemporaryError("платёж по заказу создаётся параллельно") from None

        request = PaymentRequest(
            order_id=order_id,
            attempt=payment.attempt,
            amount_kopecks=payment.amount_kopecks,
            currency=payment.currency,
            description=f"Заказ {str(order_id)[:8]}",
            return_url=self._settings.payment_return_url.format(order_id=order_id),
            correlation_id=correlation_id,
        )
        provider_payment = await self._gateway.create_payment(request)
        payment.provider_payment_id = provider_payment.provider_payment_id
        payment.pay_url = provider_payment.pay_url
        await self._session.flush()
        log.info("payment_created", order_id=str(order_id), payment_id=str(payment.id))
        return payment

    # ------------------------------------------------------------------------------------ webhook
    async def handle_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookResult:
        try:
            event = self._gateway.verify_webhook(body, headers, now=time.time())
        except InvalidSignatureError as exc:
            log.warning("webhook_invalid_signature", reason=str(exc))
            raise UnauthorizedError("Неверная подпись") from None
        except MalformedEventError:
            raise AppError("Неверный формат события", status_code=400) from None

        provider = self._gateway.name
        # Дедупликация по ID события провайдера: одно и то же событие учитывается один раз, сколько бы раз его ни прислали.
        if not await self._payments.register_event(provider, event.event_id, event.kind.value):
            log.info("webhook_duplicate", event_id=event.event_id)
            await self._session.commit()
            return WebhookResult.DUPLICATE

        payment = await self._payments.get_by_provider_id(provider, event.provider_payment_id)
        if payment is None:
            outcome = WebhookResult.IGNORED
            detail = "unknown_payment"
            log.warning("webhook_unknown_payment", provider_payment_id=event.provider_payment_id)
        elif (event.amount_kopecks, event.currency) != (payment.amount_kopecks, payment.currency):
            outcome, detail = await self._reject_mismatch(payment, event)
        elif event.kind == ProviderEventKind.SUCCEEDED:
            outcome, detail = await self._on_success(payment)
        else:
            outcome, detail = await self._on_failure(payment)

        await self._payments.set_event_outcome(provider, event.event_id, detail)
        await self._session.commit()
        return outcome

    async def _reject_mismatch(
        self, payment: Payment, event: ProviderEvent
    ) -> tuple[WebhookResult, str]:
        """Сумма или валюта в событии не совпали с платежом: статус заказа не меняем, платёж уходит на разбор."""
        await self._payments.transition(
            payment.id, {PaymentStatus.PENDING}, PaymentStatus.REVIEW_REQUIRED
        )
        await self._events.publish(
            "payment.review_required",
            {"payment_id": str(payment.id), "order_id": str(payment.order_id)},
        )
        log.error(
            "payment_amount_mismatch",
            payment_id=str(payment.id),
            expected=payment.amount_kopecks,
            received=event.amount_kopecks,
        )
        return WebhookResult.REJECTED, "amount_or_currency_mismatch"

    async def _on_success(self, payment: Payment) -> tuple[WebhookResult, str]:
        if payment.status != PaymentStatus.PENDING:
            # Платёж уже завершён раньше (например, пришло второе, новое по ID, событие о том же платеже).
            return WebhookResult.IGNORED, f"payment_already_{payment.status.value.lower()}"
        try:
            order_changed = await self._orders.change_status(payment.order_id, OrderStatus.PAID)
        except InvalidTransitionError as exc:
            order_changed = False
            reason = f"order_{exc.current.value.lower()}"
        else:
            reason = "order_already_paid"

        if order_changed:
            await self._payments.transition(
                payment.id, {PaymentStatus.PENDING}, PaymentStatus.SUCCEEDED
            )
            return WebhookResult.PROCESSED, "paid"

        # Деньги получены, а заказ оплатить нельзя (истёк, отменён или уже оплачен другим платежом).
        # Заказ НЕ воскрешаем: его товар мог достаться другому покупателю. Платёж помечаем на возврат.
        await self._payments.transition(
            payment.id, {PaymentStatus.PENDING}, PaymentStatus.REFUND_REQUIRED
        )
        await self._events.publish(
            "payment.refund_required",
            {
                "payment_id": str(payment.id),
                "order_id": str(payment.order_id),
                "amount_kopecks": payment.amount_kopecks,
                "reason": reason,
            },
        )
        log.warning("late_payment_refund_required", payment_id=str(payment.id), reason=reason)
        return WebhookResult.PROCESSED, f"refund_required_{reason}"

    async def _on_failure(self, payment: Payment) -> tuple[WebhookResult, str]:
        if payment.status != PaymentStatus.PENDING:
            return WebhookResult.IGNORED, f"payment_already_{payment.status.value.lower()}"
        await self._payments.transition(payment.id, {PaymentStatus.PENDING}, PaymentStatus.FAILED)
        # Заказ уже закрыт (отменён или истёк): отказ платежа ничего не меняет.
        with contextlib.suppress(InvalidTransitionError):
            await self._orders.change_status(payment.order_id, OrderStatus.PAYMENT_FAILED)
        return WebhookResult.PROCESSED, "payment_failed"

    # ------------------------------------------------------------------------------------ чтение
    async def latest_for_order(self, order_id: uuid.UUID) -> Payment | None:
        return await self._payments.latest_for_order(order_id)
