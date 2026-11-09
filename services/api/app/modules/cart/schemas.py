from pydantic import BaseModel, Field

from app.modules.cart.models import MAX_QUANTITY


class SetQuantityRequest(BaseModel):
    quantity: int = Field(ge=1, le=MAX_QUANTITY)


class CartLine(BaseModel):
    injector_id: int
    oem_number: str
    title: str
    unit_price_kopecks: (
        int  # ТЕКУЩАЯ цена каталога; в заказе она будет зафиксирована в момент оформления
    )
    quantity: int
    line_total_kopecks: int
    stock: int
    available: bool  # False: товар снят с продажи или остатка меньше, чем в корзине


class CartOut(BaseModel):
    items: list[CartLine]
    total_kopecks: int
    can_checkout: bool  # корзина не пуста и все позиции доступны
