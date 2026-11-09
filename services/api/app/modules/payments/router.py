import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import get_resources, get_session, get_settings
from app.core.errors import NotFoundError
from app.core.events import EventSink, get_event_sink
from app.core.lifespan import Resources
from app.modules.orders.repository import OrderRepository
from app.modules.payments.factory import build_gateway
from app.modules.payments.gateway import PaymentGateway
from app.modules.payments.service import PaymentService
from app.modules.users.deps import get_current_user
from app.modules.users.models import User

router = APIRouter(tags=["payments"])


class PaymentOut(BaseModel):
    # CREATING: заказ оформлен, платёж у провайдера ещё создаётся воркером; клиент повторяет запрос через секунду.
    status: str
    pay_url: str | None = None
    amount_kopecks: int | None = None
    currency: str | None = None


def get_payment_gateway(
    settings: Settings = Depends(get_settings), resources: Resources = Depends(get_resources)
) -> PaymentGateway:
    return build_gateway(settings, resources.http)


def get_payment_service(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    gateway: PaymentGateway = Depends(get_payment_gateway),
    events: EventSink = Depends(get_event_sink),
) -> PaymentService:
    return PaymentService(session, settings, gateway, events)


@router.post("/payments/webhook")
async def payment_webhook(
    request: Request, service: PaymentService = Depends(get_payment_service)
) -> dict[str, str]:
    """Принимает события провайдера. Защищён подписью, а не токеном пользователя.

    Любой 2xx означает «принято»: провайдер перестаёт повторять отправку. Дубль тоже отвечает 200.
    """
    # Подпись считается по сырым байтам тела, поэтому читаем их до любого разбора JSON.
    result = await service.handle_webhook(await request.body(), request.headers)
    return {"status": result.value}


@router.get("/orders/{order_id}/payment", response_model=PaymentOut)
async def order_payment(
    order_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentOut:
    """Ссылка на оплату заказа (для сайта). Видна только владельцу заказа."""
    if await OrderRepository(session).get(order_id, user_id=user.id) is None:
        raise NotFoundError("Заказ не найден")
    payment = await service.latest_for_order(order_id)
    if payment is None or payment.provider_payment_id is None:
        return PaymentOut(status="CREATING")
    return PaymentOut(
        status=payment.status.value,
        pay_url=payment.pay_url if payment.status.value == "PENDING" else None,
        amount_kopecks=payment.amount_kopecks,
        currency=payment.currency,
    )
