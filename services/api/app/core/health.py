"""Пробы здоровья.

/health/live  процесс жив (если нет, оркестратор перезапускает контейнер). Зависимости не проверяет:
              иначе недоступная БД приводила бы к бесконечным рестартам приложения.
/health/ready приложение может обслуживать запросы: доступны БД, Redis и RabbitMQ.
"""

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.core.config import Settings
from app.core.deps import get_resources, get_settings
from app.core.errors import problem_response
from app.core.lifespan import Resources

router = APIRouter(prefix="/health", tags=["health"])
log = structlog.get_logger("health")


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


async def _run_check(name: str, check: Callable[[], Awaitable[object]], limit: float) -> str:
    try:
        await asyncio.wait_for(check(), limit)
    except Exception as exc:
        # Тип ошибки пишем, текст нет: в нём бывают адреса и параметры подключения.
        log.warning("health_check_failed", dependency=name, error=type(exc).__name__)
        return "fail"
    return "ok"


@router.get("/ready")
async def ready(
    resources: Resources = Depends(get_resources), settings: Settings = Depends(get_settings)
) -> Response:
    async def check_db() -> None:
        async with resources.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def check_amqp() -> None:
        if resources.amqp.is_closed:
            raise ConnectionError("соединение с RabbitMQ закрыто")

    timeout = settings.health_check_timeout
    names = ("db", "redis", "rabbitmq")
    results = await asyncio.gather(
        _run_check("db", check_db, timeout),
        _run_check("redis", resources.redis.ping, timeout),
        _run_check("rabbitmq", check_amqp, timeout),
    )
    checks = dict(zip(names, results, strict=True))

    if all(value == "ok" for value in results):
        return JSONResponse({"status": "ok", "checks": checks})
    failed = ", ".join(name for name, value in checks.items() if value != "ok")
    return problem_response(503, f"Недоступны зависимости: {failed}", extra={"checks": checks})
