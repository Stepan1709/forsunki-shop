"""Идемпотентность HTTP: повтор запроса с тем же `Idempotency-Key` возвращает тот же ответ и не повторяет действие.

Зачем. Клиент отправил «оформить заказ», сеть оборвалась до ответа. Он не знает, создан заказ или нет, и повторяет
запрос. Без защиты получилось бы два заказа (и два резерва товара). С ключом второй запрос получает результат первого.

Как это устроено (ключ хранится в БД, а не в Redis: потеря Redis не должна приводить к дублям):
  1. `INSERT INTO idempotency_keys ... ON CONFLICT DO NOTHING` в той же транзакции, что и само действие.
  2. Вставилось (ключ новый): выполняем действие, сохраняем ответ в ту же строку, commit.
  3. Не вставилось (ключ уже есть): возвращаем сохранённый ответ; если тело запроса другое, это ошибка клиента (422).
Одновременные запросы с одним ключом: второй INSERT ждёт на уникальном индексе, пока первая транзакция не завершится.
Зафиксирована: второй видит конфликт и получает сохранённый ответ. Откатилась (например, не хватило товара): ключ
освобождается, и повтор выполнится заново (неудачные попытки не запоминаются).
"""

import hashlib
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import Header
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.errors import AppError, ConflictError

_KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{8,128}")


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_keys"

    # Ключ уникален в рамках пользователя: чужой клиент не может «занять» чужой ключ или прочитать чужой ответ.
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Отпечаток запроса (метод, путь, параметры). Тот же ключ с ДРУГИМ запросом это ошибка клиента, а не повтор.
    request_hash: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int | None] = mapped_column(Integer, default=None)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (Index("ix_idempotency_keys_created_at", "created_at"),)


def get_idempotency_key(
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> str:
    """Зависимость FastAPI: обязательный заголовок для небезопасных POST."""
    if not idempotency_key:
        raise AppError("Заголовок Idempotency-Key обязателен", status_code=400)
    if not _KEY_PATTERN.fullmatch(idempotency_key):
        raise AppError(
            "Idempotency-Key: 8-128 символов (буквы, цифры, _ . : -), рекомендуется UUID",
            status_code=400,
        )
    return idempotency_key


def fingerprint(*parts: object) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()


@dataclass(frozen=True)
class IdempotentResult:
    status: int
    body: dict[str, Any]
    replayed: bool


Operation = Callable[[], Awaitable[tuple[int, dict[str, Any]]]]


async def run_idempotent(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    key: str,
    request_hash: str,
    operation: Operation,
) -> IdempotentResult:
    """Выполняет `operation` не более одного раза на (user_id, key). Сама делает commit.

    `operation` обязана НЕ делать commit и вернуть (HTTP-статус, JSON-совместимое тело).
    """
    inserted = await session.execute(
        pg_insert(IdempotencyRecord)
        .values(user_id=user_id, key=key, request_hash=request_hash)
        .on_conflict_do_nothing(index_elements=["user_id", "key"])
    )
    if not inserted.rowcount:  # type: ignore[attr-defined]
        existing = await session.get(IdempotencyRecord, (user_id, key))
        if (
            existing is None
        ):  # строку удалили между двумя запросами: крайне редкий случай, пусть клиент повторит
            raise ConflictError("Повторите запрос")
        if existing.request_hash != request_hash:
            raise AppError("Idempotency-Key уже использован для другого запроса", status_code=422)
        if existing.response_body is None or existing.response_status is None:
            raise ConflictError("Запрос с этим ключом ещё выполняется")
        return IdempotentResult(existing.response_status, existing.response_body, replayed=True)

    status, body = await operation()
    await session.execute(
        update(IdempotencyRecord)
        .where(IdempotencyRecord.user_id == user_id, IdempotencyRecord.key == key)
        .values(response_status=status, response_body=body)
    )
    await session.commit()
    return IdempotentResult(status, body, replayed=False)
