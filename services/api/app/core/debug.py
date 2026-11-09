"""Служебные эндпоинты. Подключаются только вне prod (см. main.create_app)."""

import asyncio

from fastapi import APIRouter, Query

router = APIRouter(prefix="/debug", tags=["debug"], include_in_schema=False)


@router.get("/sleep")
async def sleep(seconds: float = Query(1.0, ge=0, le=30)) -> dict[str, float]:
    """Долгий запрос: нужен, чтобы проверять, что graceful shutdown не обрывает запросы в работе."""
    await asyncio.sleep(seconds)
    return {"slept": seconds}
