"""Формат сообщения. Меняется только обратно совместимо: поля добавляем, не удаляем и не переименовываем.

Свойства AMQP (не тело):
  message_id      UUID, создаётся ОДИН раз при создании события и не меняется при повторной отправке. По нему
                  consumer отсекает дубли (inbox).
  correlation_id  сквозной id запроса, попадает во все логи цепочки.
  type            имя события (routing key), например "user.registered".
  delivery_mode   PERSISTENT: сообщение сохраняется на диск брокера.
Тело: {"schema_version": 1, "data": {...}} в JSON, UTF-8.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractIncomingMessage

from app.core.messaging.errors import PermanentError

SCHEMA_VERSION = 1
RETRY_HEADER = "x-retry"


def build_message(
    event_type: str,
    data: dict[str, Any],
    *,
    message_id: uuid.UUID,
    correlation_id: str | None,
    headers: dict[str, Any] | None = None,
) -> Message:
    body = json.dumps({"schema_version": SCHEMA_VERSION, "data": data}, ensure_ascii=False)
    return Message(
        body=body.encode(),
        content_type="application/json",
        content_encoding="utf-8",
        delivery_mode=DeliveryMode.PERSISTENT,
        message_id=str(message_id),
        correlation_id=correlation_id,
        type=event_type,
        timestamp=datetime.now(UTC),
        headers=headers or {},
    )


@dataclass(frozen=True)
class ParsedMessage:
    message_id: uuid.UUID
    event_type: str
    correlation_id: str | None
    data: dict[str, Any]
    retry_count: int


def parse_message(message: AbstractIncomingMessage) -> ParsedMessage:
    """Разбирает входящее сообщение. Любая проблема формата = PermanentError (повтор её не исправит)."""
    try:
        message_id = uuid.UUID(message.message_id or "")
    except ValueError:
        raise PermanentError("у сообщения нет корректного message_id") from None
    try:
        body = json.loads(message.body)
    except ValueError:
        raise PermanentError("тело сообщения не является JSON") from None
    if not isinstance(body, dict):
        raise PermanentError("корень тела сообщения должен быть объектом")
    if body.get("schema_version") != SCHEMA_VERSION:
        raise PermanentError(f"неподдерживаемая версия схемы: {body.get('schema_version')!r}")
    data = body.get("data")
    if not isinstance(data, dict):
        raise PermanentError("в теле нет объекта data")
    retries = (message.headers or {}).get(RETRY_HEADER, 0)
    return ParsedMessage(
        message_id=message_id,
        event_type=message.type or "",
        correlation_id=message.correlation_id,
        data=data,
        retry_count=int(retries) if isinstance(retries, int) else 0,
    )


def get_retry_count(message: AbstractIncomingMessage) -> int:
    """Сколько повторов уже было (заголовок x-retry; нет заголовка = 0)."""
    value = (message.headers or {}).get(RETRY_HEADER, 0)
    return value if isinstance(value, int) else 0
