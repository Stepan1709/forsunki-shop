"""Реальные Postgres, Redis и RabbitMQ в контейнерах (Testcontainers), без моков."""

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aio_pika
import httpx
import pytest
import pytest_asyncio
from aio_pika.abc import AbstractChannel, AbstractRobustConnection
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.rabbitmq import RabbitMqContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.container import DockerContainer

from app.core.config import Settings
from app.core.messaging.publisher import Publisher
from app.main import create_app
from tests.helpers import make_settings

API_ROOT = Path(__file__).resolve().parents[2]
PG_IMAGE = "postgres:16.6-alpine"
REDIS_IMAGE = "redis:7.4.1-alpine"
RABBIT_IMAGE = "rabbitmq:4.0.4-management-alpine"
S3_IMAGE = "rustfs/rustfs:1.0.4"


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
def s3_container() -> Iterator[DockerContainer]:
    """S3-совместимое хранилище (RustFS). Ключи доступа совпадают с make_settings (a * 12 / s * 24)."""
    container = (
        DockerContainer(S3_IMAGE)
        .with_exposed_ports(9000)
        .with_env("RUSTFS_ACCESS_KEY", "a" * 12)
        .with_env("RUSTFS_SECRET_KEY", "s" * 24)
        .with_env("RUSTFS_ADDRESS", ":9000")
    )
    with container:
        url = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        # Контейнер «запущен» раньше, чем хранилище готово: ждём успешный /health.
        for _ in range(60):
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.TransportError:
                time.sleep(0.5)
        else:
            raise RuntimeError("S3-хранилище не запустилось")
        yield container


@pytest.fixture(scope="session")
def infra_settings(
    secrets_dir: Path,
    postgres: PostgresContainer,
    redis_container: RedisContainer,
    rabbitmq: RabbitMqContainer,
    s3_container: DockerContainer,
) -> Settings:
    s3_url = f"http://{s3_container.get_container_host_ip()}:{s3_container.get_exposed_port(9000)}"
    return make_settings(
        secrets_dir,
        s3_endpoint_url=s3_url,
        s3_public_endpoint_url=s3_url,
        postgres_host=postgres.get_container_host_ip(),
        postgres_port=int(postgres.get_exposed_port(5432)),
        redis_host=redis_container.get_container_host_ip(),
        redis_port=int(redis_container.get_exposed_port(6379)),
        rabbitmq_host=rabbitmq.get_container_host_ip(),
        rabbitmq_port=int(rabbitmq.get_exposed_port(5672)),
    )


@pytest.fixture(scope="session")
def migrated_db(infra_settings: Settings) -> None:
    """Схема БД создаётся теми же миграциями, что и в проде (а не create_all по моделям)."""
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.attributes["settings"] = infra_settings
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def app(infra_settings: Settings, migrated_db: None) -> AsyncIterator[FastAPI]:
    application = create_app(infra_settings)
    # Запускаем lifespan вручную: ASGITransport сам startup/shutdown не выполняет.
    async with application.router.lifespan_context(application):
        # Чистое состояние для каждого теста: счётчики rate limit и таблицы не должны
        # влиять друг на друга.
        resources = application.state.resources
        await resources.redis.flushdb()
        async with resources.engine.begin() as conn:
            await conn.execute(
                text(
                    "TRUNCATE users, refresh_tokens, injector_engines, injectors, engines,"
                    " manufacturers, outbox, processed_messages RESTART IDENTITY CASCADE"
                )
            )
        yield application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


# --- Пользователи и данные каталога для тестов API ---

PASSWORD = "correct-horse-battery"


async def _login_headers(
    client: httpx.AsyncClient, email: str, role: str, app: FastAPI
) -> dict[str, str]:
    await client.post("/auth/register", json={"email": email, "password": PASSWORD})
    if role == "admin":
        async with app.state.resources.engine.begin() as conn:
            await conn.execute(
                text("UPDATE users SET role = 'admin' WHERE email = :email"), {"email": email}
            )
    response = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest_asyncio.fixture
async def admin_headers(app: FastAPI, client: httpx.AsyncClient) -> dict[str, str]:
    return await _login_headers(client, "admin@example.com", "admin", app)


@pytest_asyncio.fixture
async def customer_headers(app: FastAPI, client: httpx.AsyncClient) -> dict[str, str]:
    return await _login_headers(client, "buyer@example.com", "customer", app)


@pytest_asyncio.fixture
async def catalog(app: FastAPI) -> dict[str, int]:
    """Небольшой каталог. Возвращает id нужных товаров по именам."""
    from app.modules.catalog.models import (
        Engine,
        Injector,
        InjectorCondition,
        InjectorType,
        Manufacturer,
    )

    async with app.state.resources.session_factory() as session:
        bosch = Manufacturer(name="Bosch", slug="bosch")
        denso = Manufacturer(name="Denso", slug="denso")
        om651 = Engine(name="Mercedes-Benz OM651 2.1 CDI")
        cbab = Engine(name="VW CBAB 2.0 TDI CR")
        session.add_all([bosch, denso, om651, cbab])
        await session.flush()

        def make(**kwargs: object) -> Injector:
            defaults: dict[str, object] = {
                "description": "Описание",
                "type": InjectorType.COMMON_RAIL,
                "condition": InjectorCondition.NEW,
                "price_kopecks": 4_500_000,
                "stock": 5,
            }
            return Injector(**{**defaults, **kwargs})

        bosch_new = make(
            oem_number="0 445 110 183",
            oem_normalized="0445110183",
            title="Bosch новая",
            manufacturer_id=bosch.id,
            engines=[om651],
        )
        bosch_reman = make(
            oem_number="0 445 110 183",
            oem_normalized="0445110183",
            title="Bosch восстановленная",
            manufacturer_id=bosch.id,
            condition=InjectorCondition.REMANUFACTURED,
            price_kopecks=2_700_000,
            engines=[om651],
        )
        denso_new = make(
            oem_number="095000-5550",
            oem_normalized="0950005550",
            title="Denso новая",
            manufacturer_id=denso.id,
            engines=[cbab],
        )
        bosch_pd = make(
            oem_number="0 414 720 001",
            oem_normalized="0414720001",
            title="Bosch насос-форсунка",
            manufacturer_id=bosch.id,
            type=InjectorType.PUMP_INJECTOR,
            engines=[cbab],
        )
        hidden = make(
            oem_number="0 445 999 999",
            oem_normalized="0445999999",
            title="Скрытая",
            manufacturer_id=bosch.id,
            is_active=False,
        )
        session.add_all([bosch_new, bosch_reman, denso_new, bosch_pd, hidden])
        await session.commit()
        return {
            "bosch_new": bosch_new.id,
            "bosch_reman": bosch_reman.id,
            "denso_new": denso_new.id,
            "bosch_pd": bosch_pd.id,
            "hidden": hidden.id,
        }


# --- Брокер для тестов обмена сообщениями ---


@pytest_asyncio.fixture
async def amqp(infra_settings: Settings) -> AsyncIterator[AbstractRobustConnection]:
    connection = await aio_pika.connect_robust(infra_settings.rabbitmq_url)
    yield connection
    await connection.close()


@pytest_asyncio.fixture
async def channel(amqp: AbstractRobustConnection) -> AsyncIterator[AbstractChannel]:
    ch = await amqp.channel()
    yield ch
    await ch.close()


@pytest_asyncio.fixture
async def publisher(amqp: AbstractRobustConnection) -> AsyncIterator[Publisher]:
    opened = await Publisher.open(amqp)
    yield opened
    await opened.close()


# --- Пользователи без прохождения регистрации (она ограничена rate limit'ом по IP) ---


async def create_user(
    app: FastAPI, email: str, role: str = "customer"
) -> tuple["uuid.UUID", dict[str, str]]:
    """Создаёт пользователя напрямую в БД и выпускает ему access-токен."""
    from app.core.security import create_access_token
    from app.modules.users.models import User, UserRole

    async with app.state.resources.session_factory() as session:
        user = User(email=email, password_hash="not-used-in-these-tests", role=UserRole(role))
        session.add(user)
        await session.commit()
    token = create_access_token(user.id, role, app.state.settings)
    return user.id, {"Authorization": f"Bearer {token}"}
