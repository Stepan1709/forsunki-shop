"""Служебные команды.

python -m app.cli create-admin --email you@example.com
python -m app.cli seed --count 5000 [--reset]
"""

import argparse
import asyncio
import getpass
import sys

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.cache import Cache
from app.core.config import Settings
from app.core.logging import configure_logging
from app.core.outbox import OutboxEventSink
from app.core.ratelimit import RateLimiter
from app.modules.catalog.models import Injector
from app.modules.users.models import UserRole
from app.modules.users.service import AuthService
from app.seed import reset_catalog, seed_catalog


async def create_admin(settings: Settings, email: str, password: str) -> None:
    configure_logging(settings)  # те же JSON-логи, что у остальных процессов
    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with async_sessionmaker(engine)() as session:
            service = AuthService(session, settings, RateLimiter(redis), OutboxEventSink(session))
            await service.create_user(email.strip().lower(), password, UserRole.ADMIN)
    finally:
        await redis.aclose()
        await engine.dispose()


async def seed(settings: Settings, count: int, reset: bool) -> int:
    configure_logging(settings)
    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            if reset:
                await reset_catalog(session)
            elif (await session.execute(select(func.count(Injector.id)))).scalar_one() > 0:
                raise SystemExit("Каталог не пуст. Добавьте --reset, чтобы пересоздать данные.")
            created = await seed_catalog(session, count)
        # Каталог изменился вне API: сбрасываем кэш, иначе он ещё минуты показывал бы старые данные.
        await Cache(redis).bump_version()
        return created
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="создать пользователя с ролью admin")
    admin.add_argument("--email", required=True)
    seed_parser = sub.add_parser("seed", help="заполнить каталог демо-данными")
    seed_parser.add_argument("--count", type=int, default=5000, help="примерное число форсунок")
    seed_parser.add_argument("--reset", action="store_true", help="сначала очистить каталог")
    args = parser.parse_args()

    if args.command == "create-admin":
        # Пароль вводится в терминале: в аргументах он попал бы в историю команд и список процессов.
        password = getpass.getpass("Пароль администратора: ")
        if len(password) < 8:
            sys.exit("Пароль должен быть не короче 8 символов")
        asyncio.run(create_admin(Settings(), args.email, password))  # type: ignore[call-arg]
        print("Администратор создан")
    elif args.command == "seed":
        created = asyncio.run(seed(Settings(), args.count, args.reset))  # type: ignore[call-arg]
        print(f"Создано форсунок: {created}")


if __name__ == "__main__":
    main()
