from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    (tmp_path / "postgres_password").write_text("pg/pass+1\n")
    (tmp_path / "redis_password").write_text("redis-pass")
    (tmp_path / "rabbitmq_password").write_text("mq-pass")
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
