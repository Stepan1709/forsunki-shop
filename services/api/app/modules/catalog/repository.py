"""SQL каталога. Фильтры и поиск собираются здесь, сервис видит только понятные параметры."""

import re
from dataclasses import dataclass

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import (
    Engine,
    Injector,
    InjectorCondition,
    InjectorType,
    Manufacturer,
)

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]")
MIN_SEARCH_LENGTH = 3  # триграммный индекс работает с 3 символов


def normalize_oem(value: str) -> str:
    """'0 445-110 183' -> '0445110183': пробелы, дефисы и регистр не влияют на поиск артикула."""
    return _NON_ALNUM.sub("", value).upper()


@dataclass(frozen=True)
class InjectorFilter:
    manufacturer: str | None = None  # slug производителя
    type: InjectorType | None = None
    condition: InjectorCondition | None = None
    q: str | None = None  # поиск по артикулу или применимости
    include_inactive: bool = False  # только для администратора: показывать и снятые с продажи


@dataclass(frozen=True)
class StockSnapshot:
    """Что известно о товаре в момент резервирования (цена фиксируется в заказе именно отсюда)."""

    oem_number: str
    title: str
    price_kopecks: int


class CatalogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_injectors(
        self, flt: InjectorFilter, after_id: int | None, limit: int
    ) -> list[Injector]:
        stmt = select(Injector)
        if not flt.include_inactive:
            stmt = stmt.where(Injector.is_active.is_(True))

        if flt.manufacturer:
            stmt = stmt.where(
                Injector.manufacturer_id
                == select(Manufacturer.id)
                .where(Manufacturer.slug == flt.manufacturer)
                .scalar_subquery()
            )
        if flt.type:
            stmt = stmt.where(Injector.type == flt.type)
        if flt.condition:
            stmt = stmt.where(Injector.condition == flt.condition)
        if flt.q:
            stmt = stmt.where(self._search_condition(flt.q))

        # Keyset-пагинация: «следующие N после последнего id». В отличие от OFFSET стоимость не растёт
        # с номером страницы, а добавление товаров между запросами не создаёт дублей и пропусков.
        if after_id is not None:
            stmt = stmt.where(Injector.id > after_id)
        stmt = stmt.order_by(Injector.id).limit(limit)

        result = await self._session.execute(stmt)
        return list(result.scalars().unique())

    @staticmethod
    def _search_condition(q: str):  # type: ignore[no-untyped-def]
        conditions = []
        normalized = normalize_oem(q)
        if len(normalized) >= MIN_SEARCH_LENGTH:
            # contains(autoescape=True) экранирует % и _ из пользовательского ввода.
            conditions.append(Injector.oem_normalized.contains(normalized, autoescape=True))
        text_query = q.strip()
        if len(text_query) >= MIN_SEARCH_LENGTH:
            conditions.append(
                Injector.engines.any(Engine.name.icontains(text_query, autoescape=True))
            )
        # Короткий запрос ничего не находит: возвращаем заведомо ложное условие, а не весь каталог.
        return or_(*conditions) if conditions else Injector.id < 0

    async def get_injector(
        self, injector_id: int, *, only_active: bool = True, for_update: bool = False
    ) -> Injector | None:
        stmt = select(Injector).where(Injector.id == injector_id)
        if only_active:
            stmt = stmt.where(Injector.is_active.is_(True))
        if for_update:
            # Блокировка строки: параллельное резервирование (условный UPDATE) дождётся конца правки.
            stmt = stmt.with_for_update(of=Injector)
        result = await self._session.execute(stmt)
        return result.scalars().unique().one_or_none()

    async def list_manufacturers(self) -> list[Manufacturer]:
        result = await self._session.execute(select(Manufacturer).order_by(Manufacturer.name))
        return list(result.scalars())

    async def reserve_stock(self, injector_id: int, quantity: int) -> StockSnapshot | None:
        """Резервирует товар одним условным UPDATE. None, если товара нет, он снят с продажи или не хватает.

        `WHERE stock >= :n` и вычитание выполняются в БД атомарно: два покупателя, борющиеся за последнюю
        единицу, не могут оба пройти (строка блокируется на время UPDATE, второй видит уже уменьшенный остаток).
        RETURNING отдаёт цену на момент самого резерва, поэтому заказ получает согласованную с резервом цену.
        """
        result = await self._session.execute(
            update(Injector)
            .where(
                Injector.id == injector_id,
                Injector.is_active.is_(True),
                Injector.stock >= quantity,
            )
            .values(stock=Injector.stock - quantity)
            .returning(Injector.oem_number, Injector.title, Injector.price_kopecks)
        )
        row = result.one_or_none()
        if row is None:
            return None
        return StockSnapshot(
            oem_number=row.oem_number, title=row.title, price_kopecks=row.price_kopecks
        )

    async def release_stock(self, injector_id: int, quantity: int) -> None:
        """Возвращает ранее зарезервированный товар на склад."""
        await self._session.execute(
            update(Injector)
            .where(Injector.id == injector_id)
            .values(stock=Injector.stock + quantity)
        )
