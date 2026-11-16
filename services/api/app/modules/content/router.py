from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.deps import get_cache, get_session
from app.modules.content.schemas import (
    AboutAdminOut,
    AboutOut,
    FaqAdminOut,
    FaqCreate,
    FaqUpdate,
    MoveDirection,
    PageOut,
    PageUpdate,
)
from app.modules.content.service import ContentService
from app.modules.users.deps import require_admin

# Публичная часть: страница «О нас» открыта всем, как и каталог.
router = APIRouter(prefix="/content", tags=["content"])

# Админская часть закрыта ролью admin на уровне всего роутера: забыть проверку в отдельном эндпоинте нельзя.
admin_router = APIRouter(
    prefix="/admin/content", tags=["admin"], dependencies=[Depends(require_admin)]
)


def get_content_service(
    session: AsyncSession = Depends(get_session), cache: Cache = Depends(get_cache)
) -> ContentService:
    return ContentService(session, cache)


@router.get("/about", response_model=AboutOut)
async def about(service: ContentService = Depends(get_content_service)) -> AboutOut:
    return await service.about()


@admin_router.get("", response_model=AboutAdminOut)
async def overview(service: ContentService = Depends(get_content_service)) -> AboutAdminOut:
    """Тексты блоков и все вопросы, в том числе скрытые."""
    return await service.overview()


@admin_router.put("/pages/{slug}", response_model=PageOut)
async def update_page(
    slug: str, body: PageUpdate, service: ContentService = Depends(get_content_service)
) -> PageOut:
    return await service.update_page(slug, body)


@admin_router.post("/faq", response_model=FaqAdminOut, status_code=201)
async def create_faq(
    body: FaqCreate, service: ContentService = Depends(get_content_service)
) -> FaqAdminOut:
    return await service.create_faq(body)


@admin_router.patch("/faq/{faq_id}", response_model=FaqAdminOut)
async def update_faq(
    faq_id: int, body: FaqUpdate, service: ContentService = Depends(get_content_service)
) -> FaqAdminOut:
    return await service.update_faq(faq_id, body)


@admin_router.delete("/faq/{faq_id}", status_code=204)
async def delete_faq(
    faq_id: int, service: ContentService = Depends(get_content_service)
) -> Response:
    await service.delete_faq(faq_id)
    return Response(status_code=204)


@admin_router.post("/faq/{faq_id}/move", status_code=204)
async def move_faq(
    faq_id: int,
    direction: MoveDirection = Query(description="up или down"),
    service: ContentService = Depends(get_content_service),
) -> Response:
    await service.move_faq(faq_id, direction)
    return Response(status_code=204)
