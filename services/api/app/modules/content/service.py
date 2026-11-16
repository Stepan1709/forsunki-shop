from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.errors import NotFoundError
from app.modules.content.models import PAGE_REPAIR, PAGE_SHOP, PAGE_SLUGS, FaqItem, SitePage
from app.modules.content.repository import ContentRepository
from app.modules.content.schemas import (
    AboutAdminOut,
    AboutOut,
    FaqAdminOut,
    FaqCreate,
    FaqOut,
    FaqUpdate,
    MoveDirection,
    PageOut,
    PageUpdate,
)

log = structlog.get_logger("content")

ABOUT_CACHE_KEY = "content:about"
ABOUT_CACHE_TTL_SECONDS = 300

# Тексты, пока администратор не задал свои (или если строки блока удалили из БД): страница не остаётся пустой.
DEFAULT_PAGES: dict[str, tuple[str, str]] = {
    PAGE_SHOP: (
        "О магазине",
        "Forsunki продаёт дизельные форсунки: новые и восстановленные, Bosch, Denso, Delphi и Siemens "
        "для систем Common Rail и насос-форсунок.\n\n"
        "У каждого товара указаны артикул производителя, тип, привод клапана, состояние и применимость "
        "к двигателям. Найти нужную форсунку можно по артикулу или по названию двигателя.",
    ),
    PAGE_REPAIR: (
        "Сервис ремонта",
        "Ремонтируем всю топливную аппаратуру дизельных двигателей: форсунки Common Rail и "
        "насос-форсунки, а также ТНВД (топливные насосы высокого давления).\n\n"
        "Сначала диагностика на стенде, затем согласование стоимости: без вашего согласия ремонт "
        "не начинается. Оставить заявку можно на странице «Ремонт».",
    ),
}


class ContentService:
    def __init__(self, session: AsyncSession, cache: Cache) -> None:
        self._session = session
        self._cache = cache
        self._repo = ContentRepository(session)

    # ----------------------------------------------------------------------------------- публичная часть
    async def _page(self, slug: str, stored: dict[str, SitePage]) -> PageOut:
        page = stored.get(slug)
        if page is not None:
            return PageOut(slug=slug, title=page.title, body=page.body)
        title, body = DEFAULT_PAGES[slug]
        return PageOut(slug=slug, title=title, body=body)

    async def about(self) -> AboutOut:
        """Тексты и активные вопросы. Читается при каждом открытии страницы, поэтому лежит в кэше."""

        async def load() -> dict[str, Any]:
            stored = await self._repo.pages()
            faq = await self._repo.list_faq(only_active=True)
            return AboutOut(
                shop=await self._page(PAGE_SHOP, stored),
                repair=await self._page(PAGE_REPAIR, stored),
                faq=[FaqOut(id=i.id, question=i.question, answer=i.answer) for i in faq],
            ).model_dump()

        data, _ = await self._cache.get_or_load(ABOUT_CACHE_KEY, ABOUT_CACHE_TTL_SECONDS, load)
        return AboutOut.model_validate(data)

    # ----------------------------------------------------------------------------------- администратор
    async def overview(self) -> AboutAdminOut:
        stored = await self._repo.pages()
        faq = await self._repo.list_faq(only_active=False)
        return AboutAdminOut(
            pages=[await self._page(slug, stored) for slug in PAGE_SLUGS],
            faq=[_faq_admin(i) for i in faq],
        )

    async def update_page(self, slug: str, data: PageUpdate) -> PageOut:
        if slug not in PAGE_SLUGS:
            raise NotFoundError("Такого блока нет")
        page = await self._repo.get_page(slug)
        if page is None:
            page = SitePage(slug=slug, title=data.title.strip(), body=data.body.strip())
            self._repo.add(page)
        else:
            page.title = data.title.strip()
            page.body = data.body.strip()
        await self._commit()
        log.info("about_page_updated", slug=slug)
        return PageOut(slug=slug, title=page.title, body=page.body)

    async def create_faq(self, data: FaqCreate) -> FaqAdminOut:
        item = FaqItem(
            question=data.question.strip(),
            answer=data.answer.strip(),
            is_active=data.is_active,
            position=await self._repo.next_position(),
        )
        self._repo.add(item)
        await self._commit()
        log.info("faq_created", faq_id=item.id)
        return _faq_admin(item)

    async def update_faq(self, faq_id: int, data: FaqUpdate) -> FaqAdminOut:
        item = await self._faq_or_404(faq_id)
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            if value is None:
                continue  # null означает «не менять»
            setattr(item, field, value.strip() if isinstance(value, str) else value)
        await self._commit()
        log.info("faq_updated", faq_id=faq_id, fields=sorted(changes))
        return _faq_admin(item)

    async def delete_faq(self, faq_id: int) -> None:
        item = await self._faq_or_404(faq_id)
        await self._repo.delete_faq(item)
        await self._commit()
        log.info("faq_deleted", faq_id=faq_id)

    async def move_faq(self, faq_id: int, direction: MoveDirection) -> None:
        """Меняет вопрос местами с соседним. На краю списка ничего не происходит (не ошибка)."""
        items = await self._repo.list_faq(only_active=False)
        index = next((n for n, i in enumerate(items) if i.id == faq_id), None)
        if index is None:
            raise NotFoundError("Вопрос не найден")
        other = index - 1 if direction == "up" else index + 1
        if not 0 <= other < len(items):
            return
        # Позиции обмениваются: порядок остальных вопросов не меняется. Если из-за ручной правки у двух
        # вопросов позиции совпали, соседей всё равно различает id (см. сортировку в репозитории).
        a, b = items[index], items[other]
        a.position, b.position = b.position, a.position
        if a.position == b.position:
            b.position = a.position + (1 if direction == "up" else -1)
        await self._commit()
        log.info("faq_moved", faq_id=faq_id, direction=direction)

    # ----------------------------------------------------------------------------------- внутреннее
    async def _faq_or_404(self, faq_id: int) -> FaqItem:
        item = await self._repo.get_faq(faq_id)
        if item is None:
            raise NotFoundError("Вопрос не найден")
        return item

    async def _commit(self) -> None:
        await self._session.commit()
        # Кэш сбрасывается после commit: иначе параллельный запрос мог бы снова положить в него старое.
        await self._cache.delete(ABOUT_CACHE_KEY)


def _faq_admin(item: FaqItem) -> FaqAdminOut:
    return FaqAdminOut(
        id=item.id,
        question=item.question,
        answer=item.answer,
        position=item.position,
        is_active=item.is_active,
    )
