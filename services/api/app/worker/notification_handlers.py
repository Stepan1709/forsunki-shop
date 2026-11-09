"""Обработчики событий для писем.

Одно письмо на событие обеспечивает inbox consumer'а: повторная доставка того же message_id обработчик не вызывает.
Остаётся узкое окно: SMTP принял письмо, а процесс упал до commit'а inbox. Тогда сообщение придёт снова и письмо
уйдёт второй раз. Полностью закрыть его нельзя (отправка и БД не образуют общей транзакции), поэтому выбран
режим «лучше дважды, чем ни разу», как и везде в системе.

У каждого получателя своя очередь и свой inbox-ключ: если письмо клиенту не ушло и сообщение повторяется,
мастер не получит свою копию второй раз.
"""

import uuid

from app.core.config import Settings
from app.core.mail import Mailer
from app.core.messaging.consumer import Handler, MessageContext
from app.core.messaging.errors import PermanentError
from app.modules.notifications.service import NotificationService


def _uuid(ctx: MessageContext, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(ctx.data[field]))
    except (KeyError, ValueError) as exc:
        raise PermanentError(f"В событии {ctx.event_type} нет корректного поля {field}") from exc


def make_welcome_handler(settings: Settings, mailer: Mailer) -> Handler:
    async def handler(ctx: MessageContext) -> None:
        email = ctx.data.get("email")
        if not isinstance(email, str) or not email:
            raise PermanentError("В событии user.registered нет email")
        await NotificationService(ctx.session, settings, mailer).welcome(email)

    return handler


def make_order_mail_handler(settings: Settings, mailer: Mailer, *, paid: bool) -> Handler:
    async def handler(ctx: MessageContext) -> None:
        service = NotificationService(ctx.session, settings, mailer)
        args = (_uuid(ctx, "user_id"), _uuid(ctx, "order_id"), int(ctx.data["total_kopecks"]))
        if paid:
            await service.order_paid(*args)
        else:
            await service.order_expired(*args)

    return handler


def make_repair_mail_handler(settings: Settings, mailer: Mailer, *, to_master: bool) -> Handler:
    async def handler(ctx: MessageContext) -> None:
        service = NotificationService(ctx.session, settings, mailer)
        request_id = _uuid(ctx, "request_id")
        if to_master:
            await service.repair_to_master(request_id)
        else:
            await service.repair_to_customer(request_id)

    return handler
