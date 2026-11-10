from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.deps import get_cache, get_session, get_storage
from app.core.storage import ImageStorage
from app.modules.catalog.admin_service import CatalogAdminService
from app.modules.catalog.schemas import (
    InjectorAdminCard,
    InjectorAdminPage,
    InjectorCreate,
    InjectorUpdate,
)
from app.modules.catalog.service import DEFAULT_LIMIT, MAX_LIMIT
from app.modules.users.deps import require_admin

# Весь роутер закрыт ролью admin на уровне зависимости: забыть проверку в отдельном эндпоинте нельзя.
router = APIRouter(prefix="/admin/catalog", tags=["admin"], dependencies=[Depends(require_admin)])


def get_admin_service(
    session: AsyncSession = Depends(get_session),
    cache: Cache = Depends(get_cache),
    storage: ImageStorage = Depends(get_storage),
) -> CatalogAdminService:
    return CatalogAdminService(session, cache, storage)


@router.get("/injectors", response_model=InjectorAdminPage)
async def list_injectors(
    q: str | None = Query(None, min_length=3, max_length=64),
    cursor: str | None = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    service: CatalogAdminService = Depends(get_admin_service),
) -> InjectorAdminPage:
    """Список вместе с товарами, снятыми с продажи; без кэша."""
    return await service.list_injectors(q, cursor, limit)


@router.get("/injectors/{injector_id}", response_model=InjectorAdminCard)
async def get_injector(
    injector_id: int, service: CatalogAdminService = Depends(get_admin_service)
) -> InjectorAdminCard:
    return await service.get(injector_id)


@router.post("/injectors", response_model=InjectorAdminCard, status_code=201)
async def create_injector(
    body: InjectorCreate, service: CatalogAdminService = Depends(get_admin_service)
) -> InjectorAdminCard:
    return await service.create(body)


@router.patch("/injectors/{injector_id}", response_model=InjectorAdminCard)
async def update_injector(
    injector_id: int,
    body: InjectorUpdate,
    service: CatalogAdminService = Depends(get_admin_service),
) -> InjectorAdminCard:
    return await service.update(injector_id, body)
