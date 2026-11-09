import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.modules.orders.state import OrderStatus


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # RESTRICT: пользователя с заказами нельзя удалить, история заказов не должна пропадать.
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status", values_callable=lambda e: [m.value for m in e]),
        default=OrderStatus.PENDING_PAYMENT,
    )
    total_kopecks: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # До этого момента заказ можно оплатить; потом он истекает, и резерв возвращается.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    items: Mapped[list["OrderItem"]] = relationship(
        lazy="selectin", order_by="OrderItem.injector_id", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("total_kopecks >= 0", name="total_non_negative"),
        # История заказов пользователя: по пользователю, от новых к старым.
        Index("ix_orders_user_created", "user_id", "created_at", "id"),
        # Поиск заказов, у которых вышел срок оплаты.
        Index(
            "ix_orders_pending_expires", "expires_at", postgresql_where="status = 'PENDING_PAYMENT'"
        ),
    )


class OrderItem(Base):
    """Позиция заказа. Название, артикул и цена копируются в момент оформления (снимок).

    Цену в каталоге админ может изменить когда угодно: сумма уже оформленного заказа от этого не меняется.
    """

    __tablename__ = "order_items"

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True
    )
    injector_id: Mapped[int] = mapped_column(
        ForeignKey("injectors.id", ondelete="RESTRICT"), primary_key=True
    )
    oem_number: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    unit_price_kopecks: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("unit_price_kopecks >= 0", name="unit_price_non_negative"),
    )
