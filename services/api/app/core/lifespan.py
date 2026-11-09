"""Жизненный цикл ресурсов.

Клиенты и пулы создаются один раз при старте и закрываются при остановке в обратном порядке.
Эндпоинты получают их через Depends (см. deps.py) и сами ничего не создают: иначе на каждый
запрос открывалось бы новое соединение, а при остановке часть соединений оставалась бы висеть.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import aio_pika
import httpx
import structlog
from aio_pika.abc import AbstractRobustConnection
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

log = structlog.get_logger("lifespan")


@dataclass(frozen=True)
class Resources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    amqp: AbstractRobustConnection
    http: httpx.AsyncClient


@asynccontextmanager
async def resources_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # AsyncExitStack закрывает ресурсы в обратном порядке, а если старт упал на середине,
    # уже созданные всё равно будут закрыты.
    async with AsyncExitStack() as stack:
        engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,  # проверка соединения перед выдачей: переживаем рестарт Postgres
        )
        stack.push_async_callback(engine.dispose)

        redis: Redis = Redis.from_url(settings.redis_url, decode_responses=True)
        stack.push_async_callback(redis.aclose)

        # connect_robust сам переподключается и восстанавливает каналы после обрыва.
        amqp = await aio_pika.connect_robust(
            settings.rabbitmq_url, client_properties={"connection_name": settings.service_name}
        )
        stack.push_async_callback(amqp.close)

        http = httpx.AsyncClient(timeout=settings.http_client_timeout)
        stack.push_async_callback(http.aclose)

        app.state.resources = Resources(
            engine=engine,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            redis=redis,
            amqp=amqp,
            http=http,
        )
        log.info("resources_started")
        try:
            yield
        finally:
            log.info("resources_stopping")
