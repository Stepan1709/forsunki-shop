"""Подписки воркера: какая очередь на какие события и каким обработчиком обслуживается."""

import structlog

from app.core.config import Settings
from app.core.messaging.consumer import MessageContext, Subscription
from app.core.messaging.topology import QueueSpec

log = structlog.get_logger("subscriptions")


async def handle_user_registered(ctx: MessageContext) -> None:
    """Заглушка: позже здесь появится отправка приветственного письма."""
    log.info("user_registered_received", user_id=ctx.data.get("user_id"))


def build_subscriptions(settings: Settings) -> list[Subscription]:
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
    ]
