import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from aio_pika import DeliveryMode

from app.core.messaging.errors import PermanentError
from app.core.messaging.message import build_message, get_retry_count, parse_message
from app.core.messaging.topology import QueueSpec


def incoming(**overrides: Any) -> Any:
    """Минимальная подделка входящего сообщения: parse_message читает только эти атрибуты."""
    values: dict[str, Any] = {
        "message_id": str(uuid.uuid4()),
        "type": "user.registered",
        "correlation_id": "cid-1",
        "headers": {},
        "body": json.dumps({"schema_version": 1, "data": {"user_id": "u1"}}).encode(),
    }
    return SimpleNamespace(**{**values, **overrides})


def test_built_message_has_required_properties() -> None:
    message_id = uuid.uuid4()

    message = build_message(
        "order.created", {"order_id": "o1"}, message_id=message_id, correlation_id="cid-9"
    )

    assert message.message_id == str(message_id)
    assert message.correlation_id == "cid-9"
    assert message.type == "order.created"
    assert message.delivery_mode == DeliveryMode.PERSISTENT  # сохраняется на диск брокера
    assert message.content_type == "application/json"
    assert json.loads(message.body) == {"schema_version": 1, "data": {"order_id": "o1"}}


def test_parse_roundtrip() -> None:
    message_id = uuid.uuid4()
    built = build_message("a.b", {"x": 1}, message_id=message_id, correlation_id="c")

    parsed = parse_message(incoming(message_id=built.message_id, body=built.body, type="a.b"))

    assert parsed.message_id == message_id
    assert parsed.data == {"x": 1}
    assert parsed.retry_count == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"message_id": None},  # нет message_id
        {"message_id": "not-a-uuid"},
        {"body": b"<html>"},  # не JSON
        {"body": json.dumps({"schema_version": 99, "data": {}}).encode()},  # неизвестная версия
        {"body": json.dumps({"schema_version": 1, "data": []}).encode()},  # data не объект
        {"body": json.dumps([1, 2]).encode()},  # корень не объект
    ],
)
def test_malformed_messages_are_permanent_errors(overrides: dict[str, Any]) -> None:
    with pytest.raises(PermanentError):
        parse_message(incoming(**overrides))


def test_retry_count_comes_from_header() -> None:
    assert get_retry_count(incoming(headers={"x-retry": 2})) == 2
    assert get_retry_count(incoming(headers={})) == 0
    assert get_retry_count(incoming(headers={"x-retry": "garbage"})) == 0


def test_queue_spec_names_and_retry_tiers() -> None:
    spec = QueueSpec("notifications.user_registered", ("user.registered",), (5, 30, 120))

    assert spec.dlq == "notifications.user_registered.dlq"
    assert spec.max_retries == 3
    assert spec.retry_queue(1) == "notifications.user_registered.retry.1"
    assert spec.retry_queue(3) == "notifications.user_registered.retry.3"
    assert spec.retry_queue(9) == "notifications.user_registered.retry.3"  # выше яруса не уходим
