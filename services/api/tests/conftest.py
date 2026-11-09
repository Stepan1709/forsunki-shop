from pathlib import Path

import pytest

from app.core.config import Settings
from tests.helpers import make_settings


@pytest.fixture(scope="session")
def secrets_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("secrets")


@pytest.fixture
def settings(secrets_dir: Path) -> Settings:
    """Настройки для unit-тестов (без реальной инфраструктуры)."""
    return make_settings(secrets_dir)
