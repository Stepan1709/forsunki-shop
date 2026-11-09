"""Transactional Outbox: событие записывается в БД в той же транзакции, что и бизнес-данные.

Проблема «двойной записи»: после commit отправить сообщение в брокер = две независимые операции. Упала вторая:
пользователь есть, а письмо о регистрации не уйдёт никогда. Упала первая после отправки: сообщение про
несуществующего пользователя. Outbox делает атомарной одну операцию: «пользователь + строка события» в одной
транзакции БД. Отдельный процесс (relay, app/worker/relay.py) читает неотправленные строки и публикует их в брокер.
Гарантия: at-least-once. Relay может отправить событие дважды (упал после публикации, до пометки), поэтому
получатели идемпотентны (inbox).
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.correlation import current_correlation_id
from app.core.db import Base


class OutboxMessage(Base):
    __tablename__ = "outbox"

    # id становится message_id сообщения: он создаётся один раз и не меняется при повторной отправке.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    routing_key: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    correlation_id: Mapped[str | None] = mapped_column(String(128), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Событие без получателя (нет очереди) не блокирует остальные: попытка фиксируется, следующая откладывается.
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_error: Mapped[str | None] = mapped_column(Text, default=None)

    __table_args__ = (
        # Частичный индекс: relay ищет только неотправленные, а их всегда мало. Индекс по всей таблице рос бы вместе
        # с историей, а этот остаётся маленьким.
        Index(
            "ix_outbox_unsent",
            "created_at",
            postgresql_where="sent_at IS NULL",
        ),
    )


class OutboxEventSink:
    """Реализация EventSink: кладёт событие в outbox в ТЕКУЩЕЙ сессии (commit делает вызывающий код)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def publish(self, routing_key: str, payload: dict[str, Any]) -> None:
        self._session.add(
            OutboxMessage(
                routing_key=routing_key,
                payload=payload,
                # id запроса сохраняем, чтобы по нему связать HTTP-запрос и обработку события воркером.
                correlation_id=current_correlation_id(),
            )
        )
