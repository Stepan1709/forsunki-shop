import json

import pytest
import structlog

from app.core.config import Settings
from app.core.logging import configure_logging


def test_sensitive_fields_are_redacted(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)

    structlog.get_logger("t").info("login", password="hunter2", token="abc", user="ivan")

    record = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert record["password"] == "***"
    assert record["token"] == "***"
    assert record["user"] == "ivan"


def test_correlation_id_field_present_outside_request(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(settings)
    structlog.contextvars.clear_contextvars()

    structlog.get_logger("t").info("startup")

    record = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert "correlation_id" in record
