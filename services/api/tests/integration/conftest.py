"""Реальные Postgres, Redis и RabbitMQ в контейнерах (Testcontainers), без моков."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.rabbitmq import RabbitMqContainer
from testcontainers.community.redis import RedisContainer

from app.core.config import Settings
from app.main import create_app
from tests.helpers import make_settings

PG_IMAGE = "postgres:16.6-alpine"
REDIS_IMAGE = "redis:7.4.1-alpine"
RABBIT_IMAGE = "rabbitmq:4.0.4-management-alpine"


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresContainer]:
    with PostgresContainer(
        PG_IMAGE, username="forsunki", password="test-pg", dbname="forsunki"
    ) as container:
        yield container


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer(REDIS_IMAGE, password="test-redis") as container:
        yield container


@pytest.fixture(scope="session")
def rabbitmq() -> Iterator[RabbitMqContainer]:
    with RabbitMqContainer(RABBIT_IMAGE, username="forsunki", password="test-mq") as container:
        yield container


@pytest.fixture(scope="session")
def infra_settings(
    secrets_dir: Path,
    postgres: PostgresContainer,
    redis_container: RedisContainer,
    rabbitmq: RabbitMqContainer,
) -> Settings:
    return make_settings(
        secrets_dir,
        postgres_host=postgres.get_container_host_ip(),
        postgres_port=int(postgres.get_exposed_port(5432)),
        redis_host=redis_container.get_container_host_ip(),
        redis_port=int(redis_container.get_exposed_port(6379)),
        rabbitmq_host=rabbitmq.get_container_host_ip(),
        rabbitmq_port=int(rabbitmq.get_exposed_port(5672)),
    )


@pytest_asyncio.fixture
async def app(infra_settings: Settings) -> AsyncIterator[FastAPI]:
    application = create_app(infra_settings)
    # Запускаем lifespan вручную: ASGITransport сам startup/shutdown не выполняет.
    async with application.router.lifespan_context(application):
        yield application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
