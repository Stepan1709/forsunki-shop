"""Inbox: идемпотентность consumer'а. Одно сообщение обрабатывается один раз, даже если оно пришло дважды.

Брокер гарантирует доставку «минимум один раз»: дубль возможен (relay переотправил, consumer упал до ack).
Consumer записывает message_id в таблицу и выполняет бизнес-эффект в ОДНОЙ транзакции:
  * транзакция прошла: и эффект, и пометка «обработано» сохранены;
  * транзакция откатилась: нет ни эффекта, ни пометки, поэтому повтор обработает сообщение заново.
Вторая доставка упирается в PRIMARY KEY и распознаётся как дубль.

Ключ составной (message_id, consumer): одно событие получают несколько подписчиков (у каждого своя очередь), и каждый
должен обработать его независимо. С ключом только по message_id второй подписчик принял бы событие за дубль.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    consumer: Mapped[str] = mapped_column(String(100), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


async def try_register(session: AsyncSession, message_id: uuid.UUID, consumer: str) -> bool:
    """True, если сообщение новое (запись вставлена). False, если это дубль.

    INSERT ... ON CONFLICT DO NOTHING атомарен: из двух параллельных вставок одного id успешна одна.
    """
    result = await session.execute(
        insert(ProcessedMessage)
        .values(message_id=message_id, consumer=consumer)
        .on_conflict_do_nothing(index_elements=["message_id", "consumer"])
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]
