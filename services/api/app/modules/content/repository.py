from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.content.models import FaqItem, SitePage


class ContentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def pages(self) -> dict[str, SitePage]:
        rows = (await self._session.execute(select(SitePage))).scalars()
        return {page.slug: page for page in rows}

    async def get_page(self, slug: str) -> SitePage | None:
        return await self._session.get(SitePage, slug)

    def add(self, entity: SitePage | FaqItem) -> None:
        self._session.add(entity)

    async def list_faq(self, *, only_active: bool) -> list[FaqItem]:
        stmt = select(FaqItem).order_by(FaqItem.position, FaqItem.id)
        if only_active:
            stmt = stmt.where(FaqItem.is_active.is_(True))
        return list((await self._session.execute(stmt)).scalars())

    async def get_faq(self, faq_id: int) -> FaqItem | None:
        return await self._session.get(FaqItem, faq_id)

    async def next_position(self) -> int:
        current = (await self._session.execute(select(func.max(FaqItem.position)))).scalar_one()
        return (current or 0) + 1

    async def delete_faq(self, item: FaqItem) -> None:
        await self._session.delete(item)
