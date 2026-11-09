import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class RepairStatus(enum.StrEnum):
    NEW = "NEW"  # заявка получена, мастер ещё не взял её в работу
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"
    REJECTED = "REJECTED"


class RepairRequest(Base):
    """Заявка на ремонт. Подать её может и гость, поэтому связи с users нет."""

    __tablename__ = "repair_requests"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Ключ идемпотентности хранится прямо в заявке: у гостя нет user_id, по которому работает общая таблица
    # idempotency_keys. UNIQUE на ключ не даёт создать две заявки при повторе запроса.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[RepairStatus] = mapped_column(
        Enum(RepairStatus, name="repair_status", values_callable=lambda e: [m.value for m in e]),
        default=RepairStatus.NEW,
    )
    name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(32), default=None)
    email: Mapped[str | None] = mapped_column(String(254), default=None)
    vehicle: Mapped[str] = mapped_column(String(200))  # марка, модель, двигатель
    injector: Mapped[str | None] = mapped_column(
        String(200), default=None
    )  # артикул или производитель
    description: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
