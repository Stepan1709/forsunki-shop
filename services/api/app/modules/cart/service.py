"""Корзина. Хранится в Postgres: это данные пользователя, которые не должны пропадать вместе с кэшем."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.modules.cart.repository import CartRepository
from app.modules.cart.schemas import CartLine, CartOut
from app.modules.catalog.repository import CatalogRepository


class CartService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._cart = CartRepository(session)
        self._catalog = CatalogRepository(session)

    async def set_quantity(self, user_id: uuid.UUID, injector_id: int, quantity: int) -> CartOut:
        injector = await self._catalog.get_injector(injector_id)  # только активные товары
        if injector is None:
            raise NotFoundError("Товар не найден")
        # Предварительная проверка для удобства пользователя. Окончательное решение принимает оформление заказа
        # условным UPDATE: к моменту заказа остаток может измениться.
        if quantity > injector.stock:
            raise ConflictError(f"На складе только {injector.stock} шт.")
        await self._cart.upsert(user_id, injector_id, quantity)
        await self._session.commit()
        return await self.view(user_id)

    async def remove(self, user_id: uuid.UUID, injector_id: int) -> None:
        await self._cart.remove(user_id, injector_id)  # удаление отсутствующей позиции не ошибка
        await self._session.commit()

    async def clear(self, user_id: uuid.UUID) -> None:
        await self._cart.clear(user_id)
        await self._session.commit()

    async def view(self, user_id: uuid.UUID) -> CartOut:
        lines = []
        for item in await self._cart.lines(user_id):
            injector = item.injector
            available = injector.is_active and injector.stock >= item.quantity
            lines.append(
                CartLine(
                    injector_id=injector.id,
                    oem_number=injector.oem_number,
                    title=injector.title,
                    unit_price_kopecks=injector.price_kopecks,
                    quantity=item.quantity,
                    line_total_kopecks=injector.price_kopecks * item.quantity,
                    stock=injector.stock,
                    available=available,
                )
            )
        return CartOut(
            items=lines,
            total_kopecks=sum(line.line_total_kopecks for line in lines),
            can_checkout=bool(lines) and all(line.available for line in lines),
        )
