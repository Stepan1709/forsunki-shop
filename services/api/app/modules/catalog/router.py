from fastapi import APIRouter, Depends, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.config import Settings
from app.core.deps import get_cache, get_session, get_settings, get_storage
from app.core.storage import ImageStorage
from app.modules.catalog.models import InjectorCondition, InjectorType
from app.modules.catalog.repository import MIN_SEARCH_LENGTH, InjectorFilter
from app.modules.catalog.schemas import InjectorCard, InjectorPage, ManufacturerOut
from app.modules.catalog.service import DEFAULT_LIMIT, MAX_LIMIT, CatalogService
from app.modules.users.deps import require_admin
from app.modules.users.models import User

router = APIRouter(prefix="/catalog", tags=["catalog"])


def get_catalog_service(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    cache: Cache = Depends(get_cache),
    storage: ImageStorage = Depends(get_storage),
) -> CatalogService:
    return CatalogService(session, settings, cache, storage)


def _cache_header(response: Response, hit: bool) -> None:
    # По заголовку видно, откуда пришёл ответ: удобно для проверки кэша и замеров.
    response.headers["X-Cache"] = "HIT" if hit else "MISS"


@router.get("/manufacturers", response_model=list[ManufacturerOut])
async def manufacturers(
    service: CatalogService = Depends(get_catalog_service),
) -> list[ManufacturerOut]:
    return await service.list_manufacturers()


@router.get("/injectors", response_model=InjectorPage)
async def list_injectors(
    response: Response,
    manufacturer: str | None = Query(None, description="slug производителя, например bosch"),
    type_: InjectorType | None = Query(None, alias="type"),
    condition: InjectorCondition | None = None,
    q: str | None = Query(
        None, min_length=MIN_SEARCH_LENGTH, max_length=64, description="артикул или двигатель"
    ),
    cursor: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    service: CatalogService = Depends(get_catalog_service),
) -> InjectorPage:
    flt = InjectorFilter(manufacturer=manufacturer, type=type_, condition=condition, q=q)
    page, hit = await service.list_injectors(flt, cursor, limit)
    _cache_header(response, hit)
    return page


@router.get("/injectors/{injector_id}", response_model=InjectorCard)
async def get_injector(
    injector_id: int,
    response: Response,
    service: CatalogService = Depends(get_catalog_service),
) -> InjectorCard:
    card, hit = await service.get_card(injector_id)
    _cache_header(response, hit)
    return card


@router.put("/injectors/{injector_id}/image", response_model=InjectorCard)
async def upload_injector_image(
    injector_id: int,
    file: UploadFile,
    _admin: User = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    service: CatalogService = Depends(get_catalog_service),
) -> InjectorCard:
    # Читаем на 1 байт больше лимита: так отличаем «ровно в пределах» от «слишком большой», не загружая
    # в память файл неограниченного размера.
    data = await file.read(settings.image_max_bytes + 1)
    return await service.upload_image(injector_id, data, file.content_type or "")
