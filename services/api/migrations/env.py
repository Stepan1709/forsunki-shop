"""Окружение Alembic (async). Строка подключения и логирование берутся из настроек приложения."""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app import models  # noqa: F401  (регистрирует модели в Base.metadata)
from app.core.config import Settings
from app.core.db import Base
from app.core.logging import configure_logging

config = context.config
# Тесты передают готовые настройки через config.attributes, обычный запуск читает окружение.
settings: Settings = config.attributes.get("settings") or Settings()
if not config.attributes.get("settings"):
    configure_logging(settings)  # JSON-логи миграций, как у остальных процессов

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # URL не кладём в alembic.ini: пароль в нём пришлось бы экранировать и он мог бы попасть в лог.
    engine = create_async_engine(settings.database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Офлайн-режим миграций не поддерживается")
asyncio.run(run_async_migrations())
