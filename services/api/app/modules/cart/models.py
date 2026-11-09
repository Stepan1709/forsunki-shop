import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.modules.catalog.models import Injector

MAX_QUANTITY = 99


class CartItem(Base):
    """Позиция корзины. Составной ключ (пользователь, товар): один товар лежит в корзине одной строкой."""

    __tablename__ = "cart_items"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    injector_id: Mapped[int] = mapped_column(
        ForeignKey("injectors.id", ondelete="CASCADE"), primary_key=True
    )
    quantity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    injector: Mapped[Injector] = relationship(lazy="joined")

    __table_args__ = (
        CheckConstraint(f"quantity >= 1 AND quantity <= {MAX_QUANTITY}", name="quantity_range"),
    )
