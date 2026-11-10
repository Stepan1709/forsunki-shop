"""Управление товарами для администратора: создание и правка. Список читается мимо кэша."""

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.errors import ConflictError, NotFoundError
from app.core.storage import ImageStorage
from app.modules.catalog.models import Engine, Injector, Manufacturer
from app.modules.catalog.repository import CatalogRepository, InjectorFilter, normalize_oem
from app.modules.catalog.schemas import (
    InjectorAdminCard,
    InjectorAdminItem,
    InjectorAdminPage,
    InjectorCreate,
    InjectorUpdate,
)
from app.modules.catalog.service import decode_cursor, encode_cursor

log = structlog.get_logger("catalog_admin")


class CatalogAdminService:
    def __init__(self, session: AsyncSession, cache: Cache, storage: ImageStorage) -> None:
        self._session = session
        self._cache = cache
        self._storage = storage
        self._repo = CatalogRepository(session)

    async def _to_dict(self, injector: Injector, *, details: bool) -> dict[str, Any]:
        url = await self._storage.presigned_url(injector.image_key) if injector.image_key else None
        data: dict[str, Any] = {
            "id": injector.id,
            "oem_number": injector.oem_number,
            "title": injector.title,
            "manufacturer": {
                "id": injector.manufacturer.id,
                "name": injector.manufacturer.name,
                "slug": injector.manufacturer.slug,
            },
            "type": injector.type,
            "condition": injector.condition,
            "price_kopecks": injector.price_kopecks,
            "stock": injector.stock,
            "image_url": url,
            "is_active": injector.is_active,
        }
        if details:
            data["description"] = injector.description
            data["engines"] = [{"id": e.id, "name": e.name} for e in injector.engines]
        return data

    async def list_injectors(
        self, q: str | None, cursor: str | None, limit: int
    ) -> InjectorAdminPage:
        after_id = decode_cursor(cursor) if cursor else None
        rows = await self._repo.list_injectors(
            InjectorFilter(q=q, include_inactive=True), after_id, limit + 1
        )
        page = rows[:limit]
        items = [
            InjectorAdminItem.model_validate(await self._to_dict(r, details=False)) for r in page
        ]
        next_cursor = encode_cursor(page[-1].id) if len(rows) > limit else None
        return InjectorAdminPage(items=items, next_cursor=next_cursor)

    async def get(self, injector_id: int) -> InjectorAdminCard:
        injector = await self._repo.get_injector(injector_id, only_active=False)
        if injector is None:
            raise NotFoundError("Товар не найден")
        return InjectorAdminCard.model_validate(await self._to_dict(injector, details=True))

    async def create(self, data: InjectorCreate) -> InjectorAdminCard:
        if await self._session.get(Manufacturer, data.manufacturer_id) is None:
            raise NotFoundError("Производитель не найден")
        injector = Injector(
            oem_number=data.oem_number.strip(),
            oem_normalized=normalize_oem(data.oem_number),
            title=data.title.strip(),
            description=data.description,
            manufacturer_id=data.manufacturer_id,
            type=data.type,
            condition=data.condition,
            price_kopecks=data.price_kopecks,
            stock=data.stock,
            is_active=data.is_active,
        )
        injector.engines = await self._engines(data.engines)
        self._session.add(injector)
        await self._commit_unique()
        await self._cache.bump_version()
        injector_id = injector.id
        log.info("injector_created", injector_id=injector_id)
        self._session.expire_all()
        return await self.get(injector_id)

    async def update(self, injector_id: int, data: InjectorUpdate) -> InjectorAdminCard:
        injector = await self._repo.get_injector(injector_id, only_active=False, for_update=True)
        if injector is None:
            raise NotFoundError("Товар не найден")
        changes = data.model_dump(exclude_unset=True)
        engines = changes.pop("engines", None)
        manufacturer_id = changes.get("manufacturer_id")
        if (
            manufacturer_id is not None
            and await self._session.get(Manufacturer, manufacturer_id) is None
        ):
            raise NotFoundError("Производитель не найден")
        for field, value in changes.items():
            if value is not None:  # null означает «не менять»: обязательные поля нельзя обнулить
                setattr(injector, field, value)
        if changes.get("oem_number") is not None:
            injector.oem_number = injector.oem_number.strip()
            injector.oem_normalized = normalize_oem(injector.oem_number)
        if engines is not None:
            injector.engines = await self._engines(engines)
        await self._commit_unique()
        await self._cache.bump_version()
        log.info("injector_updated", injector_id=injector_id, fields=sorted(changes))
        self._session.expire_all()
        return await self.get(injector_id)

    async def _engines(self, names: list[str]) -> list[Engine]:
        result: list[Engine] = []
        for name in dict.fromkeys(n.strip() for n in names if n.strip()):
            engine = (
                await self._session.execute(select(Engine).where(Engine.name == name))
            ).scalar_one_or_none()
            if engine is None:
                engine = Engine(name=name)
                self._session.add(engine)
            result.append(engine)
        return result

    async def _commit_unique(self) -> None:
        try:
            await self._session.commit()
        except IntegrityError:
            await self._session.rollback()
            # Единственное ограничение, которое здесь реально нарушается: артикул + состояние уже есть.
            raise ConflictError("Товар с таким артикулом и состоянием уже существует") from None
