"""Подписки воркера: какая очередь на какие события и каким обработчиком обслуживается."""

from app.core.config import Settings
from app.core.mail import Mailer
from app.core.messaging.consumer import Handler, Subscription
from app.core.messaging.topology import DelayQueueSpec, QueueSpec
from app.modules.orders.service import EXPIRE_DELAY_KEY
from app.modules.payments.gateway import PaymentGateway
from app.worker.notification_handlers import (
    make_order_closed_mail_handler,
    make_order_mail_handler,
    make_repair_mail_handler,
    make_welcome_handler,
)
from app.worker.payment_handlers import make_create_payment_handler, make_expire_order_handler

EXPIRE_KEY = "order.expire"  # ключ, под которым сообщение возвращается из очереди с задержкой


def build_subscriptions(
    settings: Settings, gateway: PaymentGateway, mailer: Mailer
) -> list[Subscription]:
    delays = tuple(settings.consumer_retry_delays_seconds)

    def notification(name: str, key: str, handler: Handler) -> Subscription:
        # Имя очереди: <подписчик>.<источник>_<событие>. У каждого письма своя очередь и свой inbox-ключ.
        return Subscription(
            spec=QueueSpec(name=name, routing_keys=(key,), retry_delays_seconds=delays),
            handler=handler,
        )

    return [
        notification(
            "notifications.user_registered",
            "user.registered",
            make_welcome_handler(settings, mailer),
        ),
        notification(
            "notifications.order_paid",
            "order.paid",
            make_order_mail_handler(settings, mailer, paid=True),
        ),
        notification(
            "notifications.order_expired",
            "order.expired",
            make_order_mail_handler(settings, mailer, paid=False),
        ),
        notification(
            "notifications.order_payment_failed",
            "order.payment_failed",
            make_order_closed_mail_handler(settings, mailer, kind="payment_failed"),
        ),
        notification(
            "notifications.order_cancelled",
            "order.cancelled",
            make_order_closed_mail_handler(settings, mailer, kind="cancelled"),
        ),
        notification(
            "notifications.repair_master",
            "repair.requested",
            make_repair_mail_handler(settings, mailer, to_master=True),
        ),
        notification(
            "notifications.repair_customer",
            "repair.requested",
            make_repair_mail_handler(settings, mailer, to_master=False),
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
