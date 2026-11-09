from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Секреты читаются из файлов /run/secrets/<имя поля> (так же, как в api).
    model_config = SettingsConfigDict(secrets_dir="/run/secrets", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    service_name: str = "payment_stub"
    log_level: str = "INFO"

    database_path: str = "/data/stub.db"
    # Адрес, по которому браузер покупателя открывает страницу оплаты (попадает в pay_url).
    public_base_url: str = "http://localhost:8002"
    # Куда провайдер шлёт события. У настоящих провайдеров этот адрес задаётся в личном кабинете.
    webhook_url: str = "http://api:8000/payments/webhook"

    # Ключ, которым api подтверждает свои запросы к провайдеру.
    payment_api_key: SecretStr
    # Общий секрет подписи webhook: по нему api убеждается, что событие прислал именно провайдер.
    payment_webhook_secret: SecretStr

    webhook_max_attempts: int = 8
    webhook_retry_base_seconds: float = 1.0  # пауза между попытками растёт: 1, 2, 4, 8 ...
    redeliver_interval_seconds: float = 10.0  # как часто искать недоставленные события
