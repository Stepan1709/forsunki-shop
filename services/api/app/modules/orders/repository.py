import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.orders.models import Order
from app.modules.orders.state import OrderStatus


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, order: Order) -> None:
        self._session.add(order)

    async def get(self, order_id: uuid.UUID, *, user_id: uuid.UUID | None = None) -> Order | None:
        """Заказ по id. С `user_id` возвращает только заказ этого пользователя (защита от IDOR).

        populate_existing: перечитать данные из БД, даже если объект уже есть в сессии (после UPDATE статуса
        объект в памяти был бы устаревшим).
        """
        stmt = select(Order).where(Order.id == order_id).execution_options(populate_existing=True)
        if user_id is not None:
            stmt = stmt.where(Order.user_id == user_id)
        return (await self._session.execute(stmt)).scalars().unique().one_or_none()

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[Order]:
        """Заказы пользователя от новых к старым. Keyset-пагинация по паре (created_at, id)."""
        stmt = select(Order).where(Order.user_id == user_id)
        if after is not None:
            # Сравнение пар: «строго раньше последнего показанного заказа».
            stmt = stmt.where(tuple_(Order.created_at, Order.id) < tuple_(*after))
        stmt = stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars().unique())

    async def transition(
        self,
        order_id: uuid.UUID,
        sources: Iterable[OrderStatus],
        target: OrderStatus,
        now: datetime,
    ) -> bool:
        """Условная смена статуса: UPDATE ... WHERE id = :id AND status IN (:sources).

        Это и защищает от гонок и двойной обработки: из двух одновременных попыток (например, «оплачен» и
        «истёк») успешна ровно одна, вторая затронет 0 строк. Возвращает True, если статус изменён нами.
        """
        values: dict[str, object] = {"status": target, "updated_at": now}
        if target == OrderStatus.PAID:
            values["paid_at"] = now
        result = await self._session.execute(
            update(Order)
            .where(Order.id == order_id, Order.status.in_(list(sources)))
            .values(**values)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]
