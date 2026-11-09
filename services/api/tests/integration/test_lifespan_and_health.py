import dataclasses

import httpx
from fastapi import Depends, FastAPI
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import Settings
from app.core.deps import get_redis, get_session
from app.core.lifespan import Resources
from app.main import create_app


async def test_resources_are_closed_on_shutdown(infra_settings: Settings) -> None:
    app = create_app(infra_settings)

    async with app.router.lifespan_context(app):
        resources: Resources = app.state.resources
        assert not resources.amqp.is_closed
        assert not resources.http.is_closed

    assert resources.amqp.is_closed
    assert resources.http.is_closed


async def test_live(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_when_all_dependencies_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"] == {"db": "ok", "redis": "ok", "rabbitmq": "ok"}


async def test_ready_fails_when_database_is_unreachable(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    # Подменяем движок на смотрящий в закрытый порт: так эмулируем падение БД,
    # не трогая общий контейнер Postgres.
    broken = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/db")
    app.state.resources = dataclasses.replace(app.state.resources, engine=broken)

    response = await client.get("/health/ready")
    live = await client.get("/health/live")
    await broken.dispose()

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["checks"] == {"db": "fail", "redis": "ok", "rabbitmq": "ok"}
    assert live.status_code == 200  # процесс жив, перезапускать его не нужно


async def test_dependencies_provide_working_resources(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    async def probe(
        session: AsyncSession = Depends(get_session), redis: Redis = Depends(get_redis)
    ) -> dict[str, object]:
        value = (await session.execute(text("SELECT 41 + 1"))).scalar_one()
        await redis.set("probe", "pong", ex=10)
        return {"db": value, "redis": await redis.get("probe")}

    app.add_api_route("/probe", probe)

    response = await client.get("/probe")

    assert response.json() == {"db": 42, "redis": "pong"}
