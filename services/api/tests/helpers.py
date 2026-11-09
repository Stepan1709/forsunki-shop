from pathlib import Path
from typing import Any

from app.core.config import Settings

# Секрет подписи webhook общий для тестов api и тестового вектора заглушки (tests/unit/test_payment_signature.py).
WEBHOOK_SECRET = "whsec_test_secret_value_0123456789"


def make_settings(secrets_dir: Path, **overrides: Any) -> Settings:
    """Настройки для тестов. Пустой каталог секретов нужен, чтобы не читать /run/secrets."""
    values: dict[str, Any] = {
        "postgres_password": "test-pg",
        "redis_password": "test-redis",
        "rabbitmq_password": "test-mq",
        "jwt_secret": "t" * 40,
        "s3_access_key": "a" * 12,
        "s3_secret_key": "s" * 24,
        "payment_api_key": "p" * 32,
        "payment_webhook_secret": WEBHOOK_SECRET,
        "env": "test",
        **overrides,
    }
    return Settings(_secrets_dir=secrets_dir, **values)  # type: ignore[call-arg]
