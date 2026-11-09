"""Бизнес-логика каталога: чтение через кэш и загрузка картинок."""

import base64
import binascii
import hashlib
import json
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.config import Settings
from app.core.errors import AppError, NotFoundError
from app.core.storage import ImageStorage
from app.modules.catalog.models import Injector
from app.modules.catalog.repository import CatalogRepository, InjectorFilter
from app.modules.catalog.schemas import (
    InjectorCard,
    InjectorListItem,
    InjectorPage,
    ManufacturerOut,
)

log = structlog.get_logger("catalog")

DEFAULT_LIMIT = 20
MAX_LIMIT = 100

# Допустимые картинки: тип -> (расширение, начало файла). Заявленному Content-Type доверять нельзя
# (его присылает клиент), поэтому проверяем ещё и «магические байты» самого файла.
_IMAGE_TYPES: dict[str, tuple[str, bytes]] = {
    "image/jpeg": ("jpg", b"\xff\xd8\xff"),
    "image/png": ("png", b"\x89PNG\r\n\x1a\n"),
    "image/webp": ("webp", b"RIFF"),
}


def encode_cursor(last_id: int) -> str:
    return base64.urlsafe_b64encode(str(last_id).encode()).decode()


def decode_cursor(cursor: str) -> int:
    try:
        value = int(base64.urlsafe_b64decode(cursor.encode()).decode())
    except (ValueError, binascii.Error):
        raise AppError("Некорректный курсор", status_code=422) from None
    if value < 0:
        raise AppError("Некорректный курсор", status_code=422)
    return value


def _injector_to_dict(injector: Injector, *, details: bool) -> dict[str, Any]:
    """Данные для кэша: только JSON-типы. Ключ картинки, а не ссылка: ссылка живёт ограниченное время."""
    data: dict[str, Any] = {
        "id": injector.id,
        "oem_number": injector.oem_number,
        "title": injector.title,
        "manufacturer": {
            "id": injector.manufacturer.id,
            "name": injector.manufacturer.name,
            "slug": injector.manufacturer.slug,
        },
        "type": injector.type.value,
        "condition": injector.condition.value,
        "price_kopecks": injector.price_kopecks,
        "stock": injector.stock,
        "image_key": injector.image_key,
    }
    if details:
        data["description"] = injector.description
        data["engines"] = [{"id": e.id, "name": e.name} for e in injector.engines]
    return data


class CatalogService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        cache: Cache,
        storage: ImageStorage,
    ) -> None:
        self._session = session
        self._settings = settings
        self._cache = cache
        self._storage = storage
        self._repo = CatalogRepository(session)

    async def _with_image_url(self, data: dict[str, Any]) -> dict[str, Any]:
        key = data.get("image_key")
        url = await self._storage.presigned_url(key) if key else None
        return {**data, "image_url": url}

    # ----------------------------------------------------------------------------------- чтение
    async def list_injectors(
        self, flt: InjectorFilter, cursor: str | None, limit: int
    ) -> tuple[InjectorPage, bool]:
        after_id = decode_cursor(cursor) if cursor else None
        version = await self._cache.version()
        params = json.dumps(
            {
                "m": flt.manufacturer,
                "t": flt.type.value if flt.type else None,
                "c": flt.condition.value if flt.condition else None,
                "q": flt.q,
                "after": after_id,
                "limit": limit,
            },
            sort_keys=True,
        )
        key = f"cat:v{version}:list:{hashlib.sha256(params.encode()).hexdigest()[:24]}"

        async def load() -> dict[str, Any]:
            # Берём на одну запись больше лимита: так узнаём, есть ли следующая страница, без COUNT(*).
            rows = await self._repo.list_injectors(flt, after_id, limit + 1)
            page_rows = rows[:limit]
            return {
                "items": [_injector_to_dict(r, details=False) for r in page_rows],
                "next_id": page_rows[-1].id if len(rows) > limit else None,
            }

        data, hit = await self._cache.get_or_load(key, self._settings.cache_list_ttl_seconds, load)
        items = [
            InjectorListItem.model_validate(await self._with_image_url(item))
            for item in data["items"]
        ]
        next_cursor = encode_cursor(data["next_id"]) if data["next_id"] is not None else None
        return InjectorPage(items=items, next_cursor=next_cursor), hit

    async def get_card(self, injector_id: int) -> tuple[InjectorCard, bool]:
        version = await self._cache.version()
        key = f"cat:v{version}:inj:{injector_id}"

        async def load() -> dict[str, Any] | None:
            injector = await self._repo.get_injector(injector_id)
            return _injector_to_dict(injector, details=True) if injector else None

        data, hit = await self._cache.get_or_load(key, self._settings.cache_card_ttl_seconds, load)
        if data is None:
            raise NotFoundError("Товар не найден")
        return InjectorCard.model_validate(await self._with_image_url(data)), hit

    async def list_manufacturers(self) -> list[ManufacturerOut]:
        rows = await self._repo.list_manufacturers()
        return [ManufacturerOut.model_validate(r) for r in rows]

    # ----------------------------------------------------------------------------------- изменение
    async def upload_image(self, injector_id: int, data: bytes, content_type: str) -> InjectorCard:
        spec = _IMAGE_TYPES.get(content_type)
        if spec is None:
            raise AppError("Допустимы только JPEG, PNG и WebP", status_code=415)
        extension, signature = spec
        if len(data) > self._settings.image_max_bytes:
            raise AppError("Файл слишком большой", status_code=413)
        if not data.startswith(signature) or (
            content_type == "image/webp" and data[8:12] != b"WEBP"
        ):
            raise AppError("Содержимое файла не соответствует его типу", status_code=415)

        injector = await self._repo.get_injector(injector_id, only_active=False)
        if injector is None:
            raise NotFoundError("Товар не найден")

        old_key = injector.image_key
        new_key = f"injectors/{injector_id}/{uuid.uuid4().hex}.{extension}"
        # Сначала файл, потом запись в БД: если загрузка упадёт, в БД не окажется ссылки на несуществующий файл.
        await self._storage.put(new_key, data, content_type)
        injector.image_key = new_key
        await self._session.commit()
        await self._cache.bump_version()
        if old_key:
            await self._storage.delete(
                old_key
            )  # старую картинку удаляем уже после успешного commit
        log.info("injector_image_updated", injector_id=injector_id)

        card, _ = await self.get_card(injector_id)
        return card
