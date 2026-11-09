import uuid

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.modules.cart.models import CartItem


class CartRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def lines(self, user_id: uuid.UUID) -> list[CartItem]:
        """Позиции корзины в стабильном порядке (по id товара).

        Порядок важен для резервирования: если все транзакции захватывают строки товаров в одном порядке, они не
        могут заблокировать друг друга по кругу (deadlock).
        """
        result = await self._session.execute(
            select(CartItem).where(CartItem.user_id == user_id).order_by(CartItem.injector_id)
        )
        return list(result.scalars().unique())

    async def upsert(self, user_id: uuid.UUID, injector_id: int, quantity: int) -> None:
        """Устанавливает количество (создаёт позицию или заменяет). Повтор запроса даёт тот же результат."""
        stmt = pg_insert(CartItem).values(
            user_id=user_id, injector_id=injector_id, quantity=quantity
        )
        await self._session.execute(
            stmt.on_conflict_do_update(
                index_elements=["user_id", "injector_id"],
                set_={"quantity": quantity, "updated_at": func.now()},
            )
        )

    async def remove(self, user_id: uuid.UUID, injector_id: int) -> None:
        await self._session.execute(
            delete(CartItem).where(CartItem.user_id == user_id, CartItem.injector_id == injector_id)
        )

    async def clear(self, user_id: uuid.UUID) -> None:
        await self._session.execute(delete(CartItem).where(CartItem.user_id == user_id))
