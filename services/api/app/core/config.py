"""Конфигурация приложения.

Несекретные настройки приходят из переменных окружения, пароли читаются из файлов
в каталоге секретов (в контейнере это /run/secrets, куда Compose монтирует `secrets:`).
Так пароли не попадают ни в `environment:`, ни в образ, ни в вывод `docker inspect`.
"""

from typing import Literal
from urllib.parse import quote

from pydantic import SecretStr, field_validator
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

    # Подпись JWT. Короткий секрет легко подобрать, поэтому длину проверяем при старте.
    jwt_secret: SecretStr
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # S3-совместимое хранилище (RustFS в dev). Ключи доступа - секреты (файлы s3_access_key/s3_secret_key).
    s3_endpoint_url: str = "http://s3:9000"  # адрес для сервера (внутри сети Docker)
    # Адрес для БРАУЗЕРА: presigned-ссылка подписывается под тем хостом, по которому её откроют.
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_bucket: str = "forsunki-images"
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_create_bucket: bool = (
        True  # создавать бакет при старте (dev/test); в prod его создаёт администратор
    )
    image_url_ttl_seconds: int = 3600
    image_max_bytes: int = 2 * 1024 * 1024

    # Кэш каталога (Redis). TTL с «разбросом» (jitter), чтобы ключи не истекали одновременно.
    cache_card_ttl_seconds: int = 600
    cache_list_ttl_seconds: int = 120
    cache_lock_ttl_seconds: int = 10

    # Воркер и обмен сообщениями (RabbitMQ).
    consumer_prefetch: int = 10  # сколько сообщений брокер выдаёт consumer'у без подтверждения
    # Паузы перед повторами (секунды); длина списка = максимум повторов. Менять можно только вместе с
    # пересозданием очередей: аргументы очереди после объявления не меняются.
    consumer_retry_delays_seconds: list[float] = [5.0, 30.0, 120.0]
    relay_batch_size: int = 50  # сколько событий outbox отправляется за одну итерацию
    relay_poll_interval_seconds: float = 0.5  # пауза, когда новых событий нет
    worker_shutdown_timeout_seconds: float = 25.0  # < stop_grace_period контейнера (30 с)
    # Метрики воркера: порт HTTP-сервера для Prometheus (None: выключено, например в тестах) и период обновления
    # значений, которые берутся из БД (заказы по статусам, состояние outbox).
    worker_metrics_port: int | None = None
    metrics_refresh_seconds: float = 15.0
    worker_heartbeat_path: str | None = "/tmp/worker-alive"  # noqa: S108  (файл-«пульс» для healthcheck)

    # Заказы: сколько минут даётся на оплату, затем резерв снимается (истечение заказа).
    order_payment_ttl_minutes: int = 60

    # Платёжный провайдер (по умолчанию заглушка payment_stub). Ключи и секрет подписи - из файлов секретов.
    payment_provider: str = "stub"
    payment_provider_url: str = "http://payment_stub:8000"
    # Куда провайдер вернёт покупателя после оплаты; {order_id} подставляется автоматически.
    payment_return_url: str = "http://localhost:8001/orders/{order_id}"
    payment_api_key: SecretStr
    payment_webhook_secret: SecretStr
    payment_webhook_tolerance_seconds: int = (
        300  # подписи старше этого срока отвергаются (защита от повторной подачи)
    )
    payment_request_timeout_seconds: float = 10.0
    # Страховка истечения заказов: воркер раз в N секунд находит просроченные заказы, даже если сообщение потерялось.
    expiry_sweep_interval_seconds: float = 60.0
    expiry_sweep_grace_seconds: float = 30.0

    # Чистка служебных таблиц (сроки хранения в днях). inbox хранится дольше всего: он защищает от повторной
    # обработки, пока сообщение ещё может прийти снова.
    retention_interval_seconds: float = 3600.0
    retention_batch_size: int = 1000
    retention_outbox_days: float = 7.0
    retention_inbox_days: float = 14.0
    retention_idempotency_days: float = 7.0

    # Письма. В dev SMTP-сервер это Mailpit (принимает письма и показывает их в веб-интерфейсе, наружу не шлёт).
    smtp_host: str = "mailpit"
    smtp_port: int = 1025
    smtp_timeout_seconds: float = 10.0
    mail_from: str = "Forsunki <noreply@forsunki.local>"
    # Кому приходят заявки на ремонт (мастеру/администратору).
    repair_notify_email: str = "repair@forsunki.local"
    # Адрес сайта для ссылок в письмах.
    shop_base_url: str = "http://localhost:8000"

    # Прокси, которым верим в X-Forwarded-For (адреса сети docker: сайт и nginx). Из внешней сети до api не добраться,
    # поэтому заголовок присылают только они. В нестандартной сети список меняется переменной TRUSTED_PROXIES.
    trusted_proxies: list[str] = [
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "::1/128",
    ]

    # Заявки на ремонт: не больше N с одного IP за окно (защита от спама).
    repair_ip_limit: int = 5
    repair_window_seconds: int = 3600

    # Трассировка (OpenTelemetry): адрес приёма OTLP по HTTP, например http://jaeger:4318. Пусто: трассировка выключена.
    otel_endpoint: str | None = None

    # Rate limiting (Redis). Окна в секундах.
    register_ip_limit: int = 5
    register_window_seconds: int = 3600
    login_ip_limit: int = 30
    login_ip_window_seconds: int = 60
    # Считаются только неудачные попытки входа для конкретного email.
    login_email_fail_limit: int = 5
    login_email_window_seconds: int = 900

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_is_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("jwt_secret должен быть не короче 32 символов")
        return value

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
