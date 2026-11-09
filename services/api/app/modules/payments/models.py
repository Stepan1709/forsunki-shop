import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class PaymentStatus(enum.StrEnum):
    PENDING = "PENDING"  # ждёт оплаты
    SUCCEEDED = "SUCCEEDED"  # деньги получены, заказ оплачен
    FAILED = "FAILED"  # провайдер отклонил платёж
    REFUND_REQUIRED = (
        "REFUND_REQUIRED"  # деньги получены, а заказ оплатить уже нельзя: нужен возврат
    )
    REVIEW_REQUIRED = (
        "REVIEW_REQUIRED"  # данные события не сошлись с платежом: нужен разбор человеком
    )


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id", ondelete="RESTRICT"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status", values_callable=lambda e: [m.value for m in e]),
        default=PaymentStatus.PENDING,
    )
    provider: Mapped[str] = mapped_column(String(32))
    provider_payment_id: Mapped[str | None] = mapped_column(String(100), default=None)
    pay_url: Mapped[str | None] = mapped_column(Text, default=None)
    # Сумма и валюта фиксируются при создании платежа. Событие провайдера сверяется с ними, а не берётся на веру.
    amount_kopecks: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        # Одна попытка оплаты заказа существует один раз; вместе с ключом провайдера order_id:attempt это даёт
        # идемпотентное создание платежа.
        UniqueConstraint("order_id", "attempt", name="uq_payments_order_attempt"),
        UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment"),
        # ГЛАВНАЯ ЗАЩИТА ОТ ДВОЙНОЙ ОПЛАТЫ НА УРОВНЕ БД: у заказа не может быть двух успешных платежей, даже если
        # ошибка в коде или гонка попытаются их создать.
        Index(
            "uq_payments_one_success_per_order",
            "order_id",
            unique=True,
            postgresql_where="status = 'SUCCEEDED'",
        ),
    )


class ProviderEvent(Base):
    """Журнал полученных событий провайдера. PRIMARY KEY (provider, event_id) отсекает повторную доставку."""

    __tablename__ = "provider_events"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(50))
    outcome: Mapped[str] = mapped_column(String(60), default="received")
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
