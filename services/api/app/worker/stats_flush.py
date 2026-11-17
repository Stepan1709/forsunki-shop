"""Перенос счётчиков просмотров из Redis в Postgres.

Сайт при каждом просмотре увеличивает счётчики в Redis (быстро, без записи в БД на каждый запрос):
  * `stats:views:<день>`        строка-счётчик: сколько страниц открыли за день;
  * `stats:product:<день>`      хеш: поле = id товара, значение = сколько раз открывали его карточку.
`<день>` это дата ГГГГ-ММ-ДД по московскому времени, её выбирает сайт.

Воркер раз в `stats_flush_interval_seconds` прибавляет накопленное к дневным итогам в Postgres и вычитает ровно
перенесённое число из Redis. Вычитание, а не удаление, нужно потому, что между чтением и вычитанием сайт мог
добавить ещё просмотры: они остаются в счётчике и уйдут в следующий проход.

Окно, которое закрыть нельзя: если процесс упал после commit, но до вычитания, то при следующем проходе те же
просмотры перенесутся второй раз. Для статистики просмотров это допустимо (погрешность в пределах одного прохода
и только при падении именно в этот момент); важнее, что просмотры не теряются.
"""

import asyncio
import contextlib
from datetime import date

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.metrics import STATS_FLUSHED
from app.modules.catalog.models import Injector
from app.modules.stats.models import ProductViewsDaily, SiteStatsDaily

log = structlog.get_logger("stats_flush")

VIEWS_PREFIX = "stats:views:"
PRODUCT_PREFIX = "stats:product:"

# Вычесть перенесённое и убрать ключ (или поле), если счётчик дошёл до нуля: иначе Redis копил бы пустые ключи.
_SUBTRACT_STRING = """
local left = redis.call('DECRBY', KEYS[1], ARGV[1])
if left <= 0 then redis.call('DEL', KEYS[1]) end
return left
"""
_SUBTRACT_FIELD = """
local left = redis.call('HINCRBY', KEYS[1], ARGV[1], -tonumber(ARGV[2]))
if left <= 0 then redis.call('HDEL', KEYS[1], ARGV[1]) end
return left
"""


def _day_of(key: str, prefix: str) -> date | None:
    """Дата из имени ключа; None, если имя не похоже на наше (такой ключ не трогаем)."""
    try:
        return date.fromisoformat(key.removeprefix(prefix))
    except ValueError:
        return None


class StatsFlusher:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self._redis = redis
        self._session_factory = session_factory
        self._settings = settings

    async def run_once(self) -> dict[str, int]:
        """Переносит всё накопленное. Возвращает, сколько просмотров перенесено (страницы и карточки)."""
        pages = await self._flush_pages()
        products = await self._flush_products()
        return {"pages": pages, "products": products}

    async def _flush_pages(self) -> int:
        total = 0
        async for raw_key in self._redis.scan_iter(match=f"{VIEWS_PREFIX}*", count=100):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            day = _day_of(key, VIEWS_PREFIX)
            raw = await self._redis.get(key)
            if day is None or raw is None or int(raw) <= 0:
                continue
            amount = int(raw)
            async with self._session_factory() as session:
                stmt = insert(SiteStatsDaily).values(day=day, page_views=amount)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["day"],
                        set_={"page_views": SiteStatsDaily.page_views + stmt.excluded.page_views},
                    )
                )
                await session.commit()
            await self._redis.eval(_SUBTRACT_STRING, 1, key, amount)
            total += amount
        if total:
            STATS_FLUSHED.labels(kind="pages").inc(total)
        return total

    async def _flush_products(self) -> int:
        total = 0
        async for raw_key in self._redis.scan_iter(match=f"{PRODUCT_PREFIX}*", count=100):
            key = raw_key.decode() if isinstance(raw_key, bytes) else raw_key
            day = _day_of(key, PRODUCT_PREFIX)
            if day is None:
                continue
            fields = {
                (f.decode() if isinstance(f, bytes) else f): int(v)
                for f, v in (await self._redis.hgetall(key)).items()
            }
            counts: dict[int, int] = {}
            for field, value in fields.items():
                if field.isdigit() and value > 0:
                    counts[int(field)] = value
            if not counts:
                continue
            async with self._session_factory() as session:
                # Просмотр несуществующего товара (удалён, выдуманный id в адресе) в БД не пишем: внешний ключ
                # не позволил бы, а одна такая строка не должна ломать перенос остальных.
                known = set(
                    (
                        await session.execute(
                            select(Injector.id).where(Injector.id.in_(list(counts)))
                        )
                    ).scalars()
                )
                for injector_id in known:
                    stmt = insert(ProductViewsDaily).values(
                        day=day, injector_id=injector_id, views=counts[injector_id]
                    )
                    await session.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["day", "injector_id"],
                            set_={"views": ProductViewsDaily.views + stmt.excluded.views},
                        )
                    )
                await session.commit()
            for injector_id, value in counts.items():
                # Вычитаем и для неизвестных товаров: иначе они повторялись бы на каждом проходе.
                await self._redis.eval(_SUBTRACT_FIELD, 1, key, str(injector_id), value)
                if injector_id in known:
                    total += value
        if total:
            STATS_FLUSHED.labels(kind="products").inc(total)
        return total

    async def run(self, stop: asyncio.Event) -> None:
        interval = self._settings.stats_flush_interval_seconds
        while not stop.is_set():
            try:
                done = await self.run_once()
                if done["pages"] or done["products"]:
                    log.info("stats_flushed", **done)
            except RedisError as exc:
                # Redis недоступен: счётчики подождут, ничего не потеряно. Следующий проход повторит.
                log.warning("stats_flush_redis_unavailable", error=type(exc).__name__)
            except Exception:
                log.exception("stats_flush_iteration_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
