"""Письма: сборка из шаблонов и отправка.

Каждое письмо - текстовая часть плюс HTML (клиенты, которые не показывают HTML, берут текст). Данные пользователя
вставляются в HTML только через автоэкранирование Jinja2: имя вроде `<script>` станет безопасным текстом.
"""

import uuid
from email.message import EmailMessage
from pathlib import Path

import structlog
from jinja2 import Environment, FileSystemLoader, StrictUndefined
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.mail import Mailer
from app.core.messaging.errors import PermanentError
from app.modules.repair.models import RepairRequest
from app.modules.users.repository import UserRepository

log = structlog.get_logger("notifications")

_TEMPLATES = Path(__file__).parent / "templates"
# StrictUndefined: забытая в шаблоне переменная приводит к ошибке, а не к пустому месту в письме клиента.
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


def format_money(kopecks: int) -> str:
    rubles, kop = divmod(kopecks, 100)
    return f"{rubles:,}".replace(",", " ") + f",{kop:02d} ₽"


def short_number(entity_id: uuid.UUID | str) -> str:
    """Короткий номер для людей: первые 8 символов id. Полный id остаётся в ссылках и в БД."""
    return str(entity_id).replace("-", "")[:8].upper()


def build_message(
    *, sender: str, to: str, subject: str, template: str, context: dict[str, object]
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(_env.get_template(f"{template}.txt.j2").render(**context))
    message.add_alternative(
        _env.get_template(f"{template}.html.j2").render(**context), subtype="html"
    )
    return message


class NotificationService:
    def __init__(self, session: AsyncSession, settings: Settings, mailer: Mailer) -> None:
        self._session = session
        self._settings = settings
        self._mailer = mailer

    def _message(self, to: str, subject: str, template: str, **context: object) -> EmailMessage:
        return build_message(
            sender=self._settings.mail_from,
            to=to,
            subject=subject,
            template=template,
            context={"shop_url": self._settings.shop_base_url.rstrip("/"), **context},
        )

    async def welcome(self, email: str) -> None:
        await self._mailer.send(self._message(email, "Добро пожаловать в Forsunki", "welcome"))

    async def order_paid(self, user_id: uuid.UUID, order_id: uuid.UUID, total_kopecks: int) -> None:
        await self._order_mail(
            user_id, order_id, total_kopecks, "order_paid", "Заказ № {number} оплачен"
        )

    async def order_expired(
        self, user_id: uuid.UUID, order_id: uuid.UUID, total_kopecks: int
    ) -> None:
        await self._order_mail(
            user_id, order_id, total_kopecks, "order_expired", "Заказ № {number} закрыт: не оплачен"
        )

    async def order_payment_failed(
        self, user_id: uuid.UUID, order_id: uuid.UUID, total_kopecks: int
    ) -> None:
        await self._order_mail(
            user_id,
            order_id,
            total_kopecks,
            "order_payment_failed",
            "Заказ № {number}: оплата не прошла",
        )

    async def order_cancelled(
        self, user_id: uuid.UUID, order_id: uuid.UUID, total_kopecks: int
    ) -> None:
        await self._order_mail(
            user_id, order_id, total_kopecks, "order_cancelled", "Заказ № {number} отменён"
        )

    async def _order_mail(
        self,
        user_id: uuid.UUID,
        order_id: uuid.UUID,
        total_kopecks: int,
        template: str,
        subject: str,
    ) -> None:
        user = await UserRepository(self._session).get_by_id(user_id)
        if user is None:
            # Пользователя удалили, пока событие лежало в очереди: писать некому, повтор не поможет.
            log.warning("notification_user_missing", user_id=str(user_id))
            return
        number = short_number(order_id)
        await self._mailer.send(
            self._message(
                user.email,
                subject.format(number=number),
                template,
                number=number,
                order_id=str(order_id),
                total=format_money(total_kopecks),
            )
        )

    async def _repair(self, request_id: uuid.UUID) -> RepairRequest:
        request = (
            await self._session.execute(select(RepairRequest).where(RepairRequest.id == request_id))
        ).scalar_one_or_none()
        if request is None:
            raise PermanentError(f"Заявка на ремонт {request_id} не найдена")
        return request

    async def repair_to_master(self, request_id: uuid.UUID) -> None:
        r = await self._repair(request_id)
        number = short_number(r.id)
        await self._mailer.send(
            self._message(
                self._settings.repair_notify_email,
                f"Заявка на ремонт № {number}",
                "repair_master",
                number=number,
                name=r.name,
                phone=r.phone,
                email=r.email,
                vehicle=r.vehicle,
                injector=r.injector,
                description=r.description,
            )
        )

    async def repair_to_customer(self, request_id: uuid.UUID) -> None:
        r = await self._repair(request_id)
        if not r.email:
            return  # клиент оставил только телефон: подтверждать нечем
        number = short_number(r.id)
        await self._mailer.send(
            self._message(
                r.email,
                f"Заявка на ремонт № {number} принята",
                "repair_customer",
                number=number,
                name=r.name,
                vehicle=r.vehicle,
            )
        )
