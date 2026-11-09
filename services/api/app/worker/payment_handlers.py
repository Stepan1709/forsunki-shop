"""Обработчики событий заказов и платежей для воркера."""

import uuid

import structlog

from app.core.config import Settings
from app.core.messaging.consumer import Handler, MessageContext
from app.core.outbox import OutboxEventSink
from app.modules.orders.service import OrderService
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.service import PaymentService

log = structlog.get_logger("handlers")


def make_create_payment_handler(settings: Settings, gateway: PaymentGateway) -> Handler:
    """order.created -> создать платёж у провайдера.

    Ошибки провайдера классифицируются самим шлюзом: недоступен -> TemporaryError (повтор с паузой),
    отклонил запрос -> PermanentError (сразу в DLQ).
    """

    async def handler(ctx: MessageContext) -> None:
        service = PaymentService(ctx.session, settings, gateway, OutboxEventSink(ctx.session))
        await service.create_for_order(uuid.UUID(ctx.data["order_id"]), ctx.correlation_id)

    return handler


def make_expire_order_handler(settings: Settings) -> Handler:
    """order.expire (сообщение вернулось из очереди с задержкой) -> закрыть неоплаченный заказ."""

    async def handler(ctx: MessageContext) -> None:
        service = OrderService(ctx.session, settings, OutboxEventSink(ctx.session))
        await service.expire_if_pending(uuid.UUID(ctx.data["order_id"]))

    return handler
