"""Служебные эндпоинты. Подключаются только вне prod (см. main.create_app)."""

import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi import status as http_status

from app.core.cache import Cache
from app.core.deps import get_cache

router = APIRouter(prefix="/debug", tags=["debug"], include_in_schema=False)


@router.get("/sleep")
async def sleep(seconds: float = Query(1.0, ge=0, le=30)) -> dict[str, float]:
    """Долгий запрос: нужен, чтобы проверять, что graceful shutdown не обрывает запросы в работе."""
    await asyncio.sleep(seconds)
    return {"slept": seconds}


@router.post("/cache/reset", status_code=http_status.HTTP_204_NO_CONTENT)
async def reset_cache(cache: Cache = Depends(get_cache)) -> None:
    """Сбрасывает кэш каталога. Нужен замерам «до/после кэша» (scripts/bench_catalog.py)."""
    await cache.bump_version()
