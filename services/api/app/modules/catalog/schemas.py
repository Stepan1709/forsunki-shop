from pydantic import BaseModel, ConfigDict

from app.modules.catalog.models import InjectorCondition, InjectorType


class ManufacturerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str


class EngineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class InjectorListItem(BaseModel):
    id: int
    oem_number: str
    title: str
    manufacturer: ManufacturerOut
    type: InjectorType
    condition: InjectorCondition
    price_kopecks: int  # цена в копейках (целое число: без ошибок округления)
    stock: int
    image_url: str | None = None  # временная ссылка на картинку, строится при каждом ответе


class InjectorCard(InjectorListItem):
    description: str
    engines: list[EngineOut]


class InjectorPage(BaseModel):
    items: list[InjectorListItem]
    # Непрозрачный курсор следующей страницы; None, если страница последняя.
    next_cursor: str | None = None
