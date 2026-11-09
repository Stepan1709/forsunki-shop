from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    (tmp_path / "postgres_password").write_text("pg/pass+1\n")
    (tmp_path / "redis_password").write_text("redis-pass")
    (tmp_path / "rabbitmq_password").write_text("mq-pass")
    (tmp_path / "jwt_secret").write_text("j" * 40)
    (tmp_path / "s3_access_key").write_text("a" * 12)
    (tmp_path / "s3_secret_key").write_text("s" * 24)
    (tmp_path / "payment_api_key").write_text("p" * 32)
    (tmp_path / "payment_webhook_secret").write_text("w" * 32)
    return tmp_path


def test_passwords_are_read_from_secret_files(secrets_dir: Path) -> None:
    settings = Settings(_secrets_dir=secrets_dir)  # type: ignore[call-arg]

    # Завершающий перевод строки из файла не должен попадать в пароль.
    assert settings.postgres_password.get_secret_value() == "pg/pass+1"
    assert settings.redis_password.get_secret_value() == "redis-pass"


def test_database_url_escapes_special_characters(secrets_dir: Path) -> None:
    settings = Settings(_secrets_dir=secrets_dir)  # type: ignore[call-arg]

    assert settings.database_url == (
        "postgresql+asyncpg://forsunki:pg%2Fpass%2B1@postgres:5432/forsunki"
    )


def test_secret_is_not_leaked_in_repr(secrets_dir: Path) -> None:
    settings = Settings(_secrets_dir=secrets_dir)  # type: ignore[call-arg]

    assert "redis-pass" not in repr(settings)


def test_missing_secret_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(_secrets_dir=tmp_path)  # type: ignore[call-arg]


def test_short_jwt_secret_is_rejected(secrets_dir: Path) -> None:
    (secrets_dir / "jwt_secret").write_text("too-short")

    with pytest.raises(ValidationError):
        Settings(_secrets_dir=secrets_dir)  # type: ignore[call-arg]
