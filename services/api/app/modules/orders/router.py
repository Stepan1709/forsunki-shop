import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import get_session, get_settings
from app.core.events import EventSink, get_event_sink
from app.core.idempotency import IdempotentResult, get_idempotency_key
from app.modules.orders.schemas import OrderOut, OrderPage
from app.modules.orders.service import DEFAULT_LIMIT, MAX_LIMIT, OrderService
from app.modules.users.deps import get_current_user
from app.modules.users.models import User

router = APIRouter(prefix="/orders", tags=["orders"])


def get_order_service(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    events: EventSink = Depends(get_event_sink),
) -> OrderService:
    return OrderService(session, settings, events)


def _respond(result: IdempotentResult) -> JSONResponse:
    # Повтор запроса помечается заголовком, чтобы клиент (и тесты) отличали «создано сейчас» от «ответ из памяти».
    headers = {"Idempotent-Replayed": "true"} if result.replayed else {}
    return JSONResponse(result.body, status_code=result.status, headers=headers)


@router.post("", response_model=OrderOut, status_code=201)
async def create_order(
    key: str = Depends(get_idempotency_key),
    user: User = Depends(get_current_user),
    service: OrderService = Depends(get_order_service),
) -> JSONResponse:
    """Оформляет заказ из корзины. Заголовок Idempotency-Key обязателен: повтор вернёт тот же заказ."""
    return _respond(await service.checkout(user.id, key))


@router.get("", response_model=OrderPage)
async def list_orders(
    cursor: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    user: User = Depends(get_current_user),
    service: OrderService = Depends(get_order_service),
) -> OrderPage:
    return await service.history(user.id, cursor, limit)


@router.get("/{order_id}", response_model=OrderOut)
async def get_order(
    order_id: uuid.UUID,
    user: User = Depends(get_current_user),
    service: OrderService = Depends(get_order_service),
) -> OrderOut:
    return await service.get(user.id, order_id)


@router.post("/{order_id}/cancel", response_model=OrderOut)
async def cancel_order(
    order_id: uuid.UUID,
    key: str = Depends(get_idempotency_key),
    user: User = Depends(get_current_user),
    service: OrderService = Depends(get_order_service),
) -> JSONResponse:
    return _respond(await service.cancel(user.id, order_id, key))
