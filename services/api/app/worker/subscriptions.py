"""Подписки воркера: какая очередь на какие события и каким обработчиком обслуживается."""

import structlog

from app.core.config import Settings
from app.core.messaging.consumer import MessageContext, Subscription
from app.core.messaging.topology import DelayQueueSpec, QueueSpec
from app.modules.orders.service import EXPIRE_DELAY_KEY
from app.modules.payments.gateway import PaymentGateway
from app.worker.payment_handlers import make_create_payment_handler, make_expire_order_handler

log = structlog.get_logger("subscriptions")

EXPIRE_KEY = "order.expire"  # ключ, под которым сообщение возвращается из очереди с задержкой


async def handle_user_registered(ctx: MessageContext) -> None:
    """Заглушка: позже здесь появится отправка приветственного письма."""
    log.info("user_registered_received", user_id=ctx.data.get("user_id"))


def build_subscriptions(settings: Settings, gateway: PaymentGateway) -> list[Subscription]:
    delays = tuple(settings.consumer_retry_delays_seconds)
    return [
        Subscription(
            spec=QueueSpec(
                # Имя очереди: <подписчик>.<источник>_<событие>.
                name="notifications.user_registered",
                routing_keys=("user.registered",),
                retry_delays_seconds=delays,
            ),
            handler=handle_user_registered,
        ),
        Subscription(
            spec=QueueSpec(
                name="payments.order_created",
                routing_keys=("order.created",),
                retry_delays_seconds=delays,
            ),
            handler=make_create_payment_handler(settings, gateway),
        ),
        Subscription(
            spec=QueueSpec(
                name="orders.order_expire",
                routing_keys=(EXPIRE_KEY,),
                retry_delays_seconds=delays,
            ),
            handler=make_expire_order_handler(settings),
        ),
    ]


def build_delay_queues(settings: Settings) -> list[DelayQueueSpec]:
    """Очередь с задержкой для истечения заказов: TTL равен сроку, отведённому на оплату."""
    return [
        DelayQueueSpec(
            name="orders.expire_delay",
            bind_key=EXPIRE_DELAY_KEY,
            dead_letter_key=EXPIRE_KEY,
            ttl_seconds=settings.order_payment_ttl_minutes * 60,
        )
    ]
