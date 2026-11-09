"""Служебные команды. Запуск: `python -m app.cli create-admin --email you@example.com`."""

import argparse
import asyncio
import getpass
import sys

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.core.events import LoggingEventSink
from app.core.logging import configure_logging
from app.core.ratelimit import RateLimiter
from app.modules.users.models import UserRole
from app.modules.users.service import AuthService


async def create_admin(settings: Settings, email: str, password: str) -> None:
    configure_logging(settings)  # те же JSON-логи, что у остальных процессов

    engine = create_async_engine(settings.database_url)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    try:
        async with async_sessionmaker(engine)() as session:
            service = AuthService(session, settings, RateLimiter(redis), LoggingEventSink())
            await service.create_user(email.strip().lower(), password, UserRole.ADMIN)
    finally:
        await redis.aclose()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="создать пользователя с ролью admin")
    admin.add_argument("--email", required=True)
    args = parser.parse_args()

    if args.command == "create-admin":
        # Пароль вводится в терминале: в аргументах он попал бы в историю команд и список процессов.
        password = getpass.getpass("Пароль администратора: ")
        if len(password) < 8:
            sys.exit("Пароль должен быть не короче 8 символов")
        asyncio.run(create_admin(Settings(), args.email, password))  # type: ignore[call-arg]
        print("Администратор создан")


if __name__ == "__main__":
    main()
