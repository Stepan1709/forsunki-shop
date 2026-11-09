import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field

from app.modules.orders.state import OrderStatus


class OrderItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    injector_id: int
    oem_number: str
    title: str
    unit_price_kopecks: int  # цена на момент оформления (снимок), а не текущая цена каталога
    quantity: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def line_total_kopecks(self) -> int:
        return self.unit_price_kopecks * self.quantity


class OrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: OrderStatus
    total_kopecks: int
    currency: str
    created_at: datetime
    expires_at: datetime
    paid_at: datetime | None
    items: list[OrderItemOut]


class OrderPage(BaseModel):
    items: list[OrderOut]
    next_cursor: str | None = None
