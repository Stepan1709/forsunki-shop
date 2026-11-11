"""Жизненный цикл ресурсов.

Клиенты и пулы создаются один раз при старте и закрываются при остановке в обратном порядке.
Эндпоинты получают их через Depends (см. deps.py) и сами ничего не создают: иначе на каждый
запрос открывалось бы новое соединение, а при остановке часть соединений оставалась бы висеть.
"""

import functools
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

import aio_pika
import aioboto3
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
from app.core.storage import ImageStorage
from app.core.tracing import shutdown_tracing

log = structlog.get_logger("lifespan")


@dataclass(frozen=True)
class Resources:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    amqp: AbstractRobustConnection
    http: httpx.AsyncClient
    storage: ImageStorage


@asynccontextmanager
async def resources_lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    # AsyncExitStack закрывает ресурсы в обратном порядке, а если старт упал на середине,
    # уже созданные всё равно будут закрыты.
    async with AsyncExitStack() as stack:
        # Регистрируется первым, значит закрывается последним: спаны остановки тоже успеют уйти в Jaeger.
        stack.callback(shutdown_tracing)
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

        storage = await _create_storage(stack, settings)

        app.state.resources = Resources(
            engine=engine,
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            redis=redis,
            amqp=amqp,
            http=http,
            storage=storage,
        )
        from app.admin import setup_admin  # локальный импорт: admin.py сам импортирует lifespan

        setup_admin(app)
        log.info("resources_started")
        try:
            yield
        finally:
            log.info("resources_stopping")


@functools.cache
def _s3_session() -> aioboto3.Session:
    # Одна сессия на процесс: при первом создании клиента botocore читает описания S3-API с диска
    # (на медленной файловой системе до 10 секунд), дальше они берутся из памяти.
    return aioboto3.Session()


async def _create_storage(stack: AsyncExitStack, settings: Settings) -> ImageStorage:
    session = _s3_session()
    common = {
        "region_name": settings.s3_region,
        "aws_access_key_id": settings.s3_access_key.get_secret_value(),
        "aws_secret_access_key": settings.s3_secret_key.get_secret_value(),
    }
    # Клиент aioboto3 это async-контекстный менеджер: регистрируем его в стопке закрытия.
    client = await stack.enter_async_context(
        session.client("s3", endpoint_url=settings.s3_endpoint_url, **common)
    )
    signing_client = await stack.enter_async_context(
        session.client("s3", endpoint_url=settings.s3_public_endpoint_url, **common)
    )
    storage = ImageStorage(
        client, signing_client, settings.s3_bucket, settings.image_url_ttl_seconds
    )
    if settings.s3_create_bucket:
        await storage.ensure_bucket()
    return storage
