"""Конфигурация сайта. Несекретное из переменных окружения, пароль Redis из файла секрета."""

from typing import Literal
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "web"
    log_level: str = "INFO"

    # Бэкенд. Сайт ходит в него по HTTP и не знает ничего про его БД.
    api_url: str = "http://api:8000"
    api_timeout_seconds: float = 10.0

    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: SecretStr

    # Сессия: в cookie только случайный id, токены лежат в Redis. Срок продлевается при каждом запросе.
    session_cookie_name: str = "sid"
    session_ttl_seconds: int = 60 * 60 * 24 * 7
    anonymous_session_ttl_seconds: int = 60 * 60 * 2
    # В prod сайт отдаётся по HTTPS (nginx), тогда cookie помечается Secure.
    cookie_secure: bool = False

    # Контакты и тексты (позже заменяются реальными).
    shop_name: str = "Forsunki"
    shop_phone: str = "+7 000 000-00-00"
    shop_email: str = "repair@forsunki.local"
    shop_address: str = "г. Москва, ул. Примерная, 1 (мастерская)"
    shop_hours: str = "Пн-Сб, 9:00-19:00"

    @property
    def redis_url(self) -> str:
        password = quote(self.redis_password.get_secret_value(), safe="")
        return f"redis://:{password}@{self.redis_host}:{self.redis_port}/1"
