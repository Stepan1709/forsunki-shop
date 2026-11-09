"""Конфигурация приложения.

Несекретные настройки приходят из переменных окружения, пароли читаются из файлов
в каталоге секретов (в контейнере это /run/secrets, куда Compose монтирует `secrets:`).
Так пароли не попадают ни в `environment:`, ни в образ, ни в вывод `docker inspect`.
"""

from typing import Literal
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Имя поля = имя файла секрета (например, postgres_password -> /run/secrets/postgres_password).
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "api"
    log_level: str = "INFO"

    # Пул соединений с БД: создаётся один раз в lifespan и живёт всё время работы процесса.
    db_pool_size: int = 10
    db_max_overflow: int = 5
    # Таймаут одной проверки в /health/ready: зависшая БД не должна вешать сам healthcheck.
    health_check_timeout: float = 2.0
    http_client_timeout: float = 10.0

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str = "forsunki"
    postgres_db: str = "forsunki"
    postgres_password: SecretStr

    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: SecretStr

    rabbitmq_host: str = "rabbitmq"
    rabbitmq_port: int = 5672
    rabbitmq_user: str = "forsunki"
    rabbitmq_password: SecretStr

    # Пароли кодируем: спецсимволы в пароле иначе сломали бы разбор URL.
    @property
    def database_url(self) -> str:
        password = quote(self.postgres_password.get_secret_value(), safe="")
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        return f"redis://:{password}@{self.redis_host}:{self.redis_port}/0"

    @property
    def rabbitmq_url(self) -> str:
        password = quote(self.rabbitmq_password.get_secret_value(), safe="")
        return f"amqp://{self.rabbitmq_user}:{password}@{self.rabbitmq_host}:{self.rabbitmq_port}/"
