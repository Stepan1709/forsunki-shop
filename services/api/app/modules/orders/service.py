"""Оформление, отмена и смена статуса заказа."""

import base64
import binascii
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.core.events import EventSink
from app.core.idempotency import IdempotentResult, fingerprint, run_idempotent
from app.modules.cart.repository import CartRepository
from app.modules.catalog.repository import CatalogRepository
from app.modules.orders.models import Order, OrderItem
from app.modules.orders.repository import OrderRepository
from app.modules.orders.schemas import OrderOut, OrderPage
from app.modules.orders.state import (
    EVENT_FOR_STATUS,
    STOCK_RELEASING,
    InvalidTransitionError,
    OrderStatus,
    sources_for,
)

log = structlog.get_logger("orders")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100
EXPIRE_DELAY_KEY = "order.expire.delay"  # ключ, под которым событие попадает в очередь с задержкой


def encode_cursor(created_at: datetime, order_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{order_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created, order_id = raw.split("|")
        return datetime.fromisoformat(created), uuid.UUID(order_id)
    except (ValueError, binascii.Error):
        raise AppError("Некорректный курсор", status_code=422) from None


def _event_payload(order: Order, *, with_items: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "order_id": str(order.id),
        "user_id": str(order.user_id),
        "status": order.status.value,
        "total_kopecks": order.total_kopecks,
        "currency": order.currency,
    }
    if with_items:
        payload["items"] = [
            {"injector_id": i.injector_id, "quantity": i.quantity} for i in order.items
        ]
    return payload


class OrderService:
    def __init__(self, session: AsyncSession, settings: Settings, events: EventSink) -> None:
        self._session = session
        self._settings = settings
        self._events = events
        self._orders = OrderRepository(session)
        self._cart = CartRepository(session)
        self._catalog = CatalogRepository(session)

    # ------------------------------------------------------------------------------------ оформление
    async def checkout(self, user_id: uuid.UUID, key: str) -> IdempotentResult:
        return await run_idempotent(
            self._session,
            user_id=user_id,
            key=key,
            request_hash=fingerprint("POST", "/orders", user_id),
            operation=lambda: self._create_order(user_id),
        )

    async def _create_order(self, user_id: uuid.UUID) -> tuple[int, dict[str, Any]]:
        """Всё в одной транзакции: резерв, заказ, очистка корзины, событие. Любая ошибка откатывает всё."""
        lines = await self._cart.lines(user_id)
        if not lines:
            raise AppError("Корзина пуста", status_code=422)

        items: list[OrderItem] = []
        for line in lines:  # порядок по id товара (см. CartRepository.lines)
            snapshot = await self._catalog.reserve_stock(line.injector_id, line.quantity)
            if snapshot is None:
                # Откат всей транзакции вернёт и уже зарезервированные позиции: частичных заказов не бывает.
                raise ConflictError(
                    f"Недостаточно товара на складе: {line.injector.oem_number} "
                    f"(запрошено {line.quantity})"
                )
            items.append(
                OrderItem(
                    injector_id=line.injector_id,
                    oem_number=snapshot.oem_number,
                    title=snapshot.title,
                    unit_price_kopecks=snapshot.price_kopecks,
                    quantity=line.quantity,
                )
            )

        now = datetime.now(UTC)
        order = Order(
            user_id=user_id,
            status=OrderStatus.PENDING_PAYMENT,
            total_kopecks=sum(i.unit_price_kopecks * i.quantity for i in items),
            currency="RUB",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(minutes=self._settings.order_payment_ttl_minutes),
            items=items,
        )
        self._orders.add(order)
        await self._session.flush()
        await self._events.publish("order.created", _event_payload(order, with_items=True))
        # Отложенное истечение: событие попадёт в очередь с TTL (см. worker), по истечении срока оплаты вернётся
        # как order.expire, и неоплаченный заказ будет закрыт, а товар возвращён на склад.
        await self._events.publish(EXPIRE_DELAY_KEY, {"order_id": str(order.id)})
        await self._cart.clear(user_id)
        await self._session.flush()
        log.info("order_created", order_id=str(order.id), user_id=str(user_id))
        return 201, OrderOut.model_validate(order).model_dump(mode="json")

    # ------------------------------------------------------------------------------------ отмена
    async def cancel(self, user_id: uuid.UUID, order_id: uuid.UUID, key: str) -> IdempotentResult:
        return await run_idempotent(
            self._session,
            user_id=user_id,
            key=key,
            request_hash=fingerprint("POST", f"/orders/{order_id}/cancel", user_id),
            operation=lambda: self._cancel(user_id, order_id),
        )

    async def _cancel(self, user_id: uuid.UUID, order_id: uuid.UUID) -> tuple[int, dict[str, Any]]:
        order = await self._orders.get(order_id, user_id=user_id)
        if order is None:
            raise NotFoundError("Заказ не найден")  # чужой заказ неотличим от несуществующего
        await self.change_status(order_id, OrderStatus.CANCELLED)
        refreshed = await self._orders.get(order_id, user_id=user_id)
        if refreshed is None:
            raise NotFoundError("Заказ не найден")
        return 200, OrderOut.model_validate(refreshed).model_dump(mode="json")

    # ------------------------------------------------------------------------------------ смена статуса
    async def apply_transition(self, order_id: uuid.UUID, target: OrderStatus) -> bool:
        """Системная смена статуса (оплата, истечение, отказ платежа). Возвращает True, если статус изменён.

        Повтор того же перехода безопасен и возвращает False: событие может прийти дважды.
        """
        changed = await self.change_status(order_id, target)
        await self._session.commit()
        return changed

    async def change_status(self, order_id: uuid.UUID, target: OrderStatus) -> bool:
        now = datetime.now(UTC)
        if not await self._orders.transition(order_id, sources_for(target), target, now):
            current = await self._orders.get(order_id)
            if current is None:
                raise NotFoundError("Заказ не найден")
            if current.status == target:
                return False  # уже в нужном статусе: повтор, ничего не делаем
            raise InvalidTransitionError(current.status, target)

        order = await self._orders.get(order_id)
        if (
            order is None
        ):  # заказ удалили между шагами: невозможно при RESTRICT, но не прячем это за assert
            raise NotFoundError("Заказ не найден")
        if target in STOCK_RELEASING:
            # Возврат на склад выполняется только тем, кто реально сменил статус (условный UPDATE выше),
            # поэтому при гонке двух отмен товар вернётся ровно один раз.
            for item in order.items:  # уже отсортированы по injector_id
                await self._catalog.release_stock(item.injector_id, item.quantity)
        await self._events.publish(EVENT_FOR_STATUS[target], _event_payload(order))
        log.info("order_status_changed", order_id=str(order_id), status=target.value)
        return True

    # ------------------------------------------------------------------------------------ чтение
    async def get(self, user_id: uuid.UUID, order_id: uuid.UUID) -> OrderOut:
        order = await self._orders.get(order_id, user_id=user_id)
        if order is None:
            raise NotFoundError("Заказ не найден")
        return OrderOut.model_validate(order)

    async def history(self, user_id: uuid.UUID, cursor: str | None, limit: int) -> OrderPage:
        after = decode_cursor(cursor) if cursor else None
        rows = await self._orders.list_for_user(user_id, after, limit + 1)
        page = rows[:limit]
        next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
        return OrderPage(items=[OrderOut.model_validate(o) for o in page], next_cursor=next_cursor)

    async def expire_if_pending(self, order_id: uuid.UUID) -> bool:
        """Закрывает неоплаченный заказ по истечении срока. Коммит выполняет вызывающий код.

        Заказ мог уже быть оплачен, отменён или закрыт: это не ошибка, истечение просто ничего не делает.
        """
        try:
            changed = await self.change_status(order_id, OrderStatus.EXPIRED)
        except InvalidTransitionError as exc:
            log.info("order_not_expirable", order_id=str(order_id), status=exc.current.value)
            return False
        except NotFoundError:
            log.warning("order_to_expire_not_found", order_id=str(order_id))
            return False
        return changed

    async def expired_pending_ids(self, grace_seconds: float, limit: int) -> list[uuid.UUID]:
        """Заказы, у которых срок оплаты давно вышел, а статус всё ещё PENDING_PAYMENT (для страховочного обхода)."""
        return await self._orders.expired_pending_ids(
            datetime.now(UTC) - timedelta(seconds=grace_seconds), limit
        )
