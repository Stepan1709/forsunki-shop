"""Тесты сайта: настоящий Redis (Testcontainers) для сессий, а бэкенд api подменяется на уровне HTTP."""

import re
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr
from testcontainers.community.redis import RedisContainer

from app.config import Settings
from app.main import create_app
from tests.fake_api import FakeApi

PASSWORD = "pass-12345"


@pytest.fixture(scope="session")
def redis_container() -> Iterator[RedisContainer]:
    with RedisContainer("redis:7.4.1-alpine", password="test-redis") as container:
        yield container


@pytest.fixture
def settings(tmp_path: Path, redis_container: RedisContainer) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _secrets_dir=tmp_path,
        redis_password=SecretStr("test-redis"),
        redis_host=redis_container.get_container_host_ip(),
        redis_port=int(redis_container.get_exposed_port(6379)),
        env="test",
    )


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest_asyncio.fixture
async def app(settings: Settings, api: FakeApi) -> AsyncIterator[FastAPI]:
    application = create_app(settings, api_transport=api.transport())
    async with application.router.lifespan_context(application):
        await application.state.redis.flushdb()
        yield application


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as c:
        yield c


def csrf_of(html: str) -> str:
    """Токен CSRF из скрытого поля формы на странице."""
    found = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert found, "на странице нет csrf_token"
    return found.group(1)


async def get_token(client: httpx.AsyncClient) -> str:
    return csrf_of((await client.get("/login")).text)


async def login(
    client: httpx.AsyncClient, email: str = "buyer@example.com", password: str = PASSWORD
) -> httpx.Response:
    token = await get_token(client)
    return await client.post(
        "/login", data={"email": email, "password": password, "csrf_token": token, "next": "/"}
    )


@pytest_asyncio.fixture
async def buyer(client: httpx.AsyncClient) -> httpx.AsyncClient:
    response = await login(client)
    assert response.status_code == 303
    return client


@pytest_asyncio.fixture
async def admin(client: httpx.AsyncClient) -> httpx.AsyncClient:
    response = await login(client, "admin@example.com")
    assert response.status_code == 303
    return client
