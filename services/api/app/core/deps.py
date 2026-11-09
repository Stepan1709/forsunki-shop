"""Зависимости FastAPI: доступ к ресурсам из lifespan."""

from collections.abc import AsyncIterator
from typing import cast

import httpx
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.lifespan import Resources


def get_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


def get_resources(request: Request) -> Resources:
    return cast(Resources, request.app.state.resources)


async def get_session(resources: Resources = Depends(get_resources)) -> AsyncIterator[AsyncSession]:
    # Одна сессия на запрос. Закрытие без commit откатывает транзакцию, поэтому commit
    # делает сервисный слой явно, а не зависимость втихую.
    async with resources.session_factory() as session:
        yield session


def get_redis(resources: Resources = Depends(get_resources)) -> Redis:
    return resources.redis


def get_http_client(resources: Resources = Depends(get_resources)) -> httpx.AsyncClient:
    return resources.http
