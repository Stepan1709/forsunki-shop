import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from app.core.config import Settings
from tests.helpers import make_settings

API_ROOT = Path(__file__).resolve().parents[2]


async def _current_revision(settings: Settings) -> str | None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT version_num FROM alembic_version"))
            version: str | None = result.scalar_one_or_none()
            return version
    finally:
        await engine.dispose()


# Обычный (не async) тест: env.py Alembic сам запускает asyncio.run, а внутри
# работающего event loop это невозможно.
def test_upgrade_downgrade_upgrade(tmp_path: Path) -> None:
    with PostgresContainer(
        "postgres:16.6-alpine", username="forsunki", password="test-pg", dbname="forsunki"
    ) as pg:
        settings = make_settings(
            tmp_path,
            postgres_host=pg.get_container_host_ip(),
            postgres_port=int(pg.get_exposed_port(5432)),
        )
        cfg = Config(str(API_ROOT / "alembic.ini"))
        cfg.attributes["settings"] = settings

        command.upgrade(cfg, "head")
        assert asyncio.run(_current_revision(settings)) == "0004"
        # Модели и миграции не расходятся: autogenerate не находит отличий.
        command.check(cfg)

        command.downgrade(cfg, "base")
        # После отката до base таблица версий остаётся, но пустая.
        assert asyncio.run(_current_revision(settings)) is None

        command.upgrade(cfg, "head")  # повторное применение после отката
        assert asyncio.run(_current_revision(settings)) == "0004"
