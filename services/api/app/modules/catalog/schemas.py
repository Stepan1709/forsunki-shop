from pydantic import BaseModel, ConfigDict, Field

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


# ----------------------------------------------------------------------------------- администратор
class InjectorAdminItem(InjectorListItem):
    is_active: bool


class InjectorAdminCard(InjectorCard):
    is_active: bool


class InjectorAdminPage(BaseModel):
    items: list[InjectorAdminItem]
    next_cursor: str | None = None


class InjectorCreate(BaseModel):
    oem_number: str = Field(min_length=3, max_length=64)
    title: str = Field(min_length=2, max_length=200)
    description: str = Field(default="", max_length=5000)
    manufacturer_id: int
    type: InjectorType
    condition: InjectorCondition
    price_kopecks: int = Field(ge=0, le=100_000_000)
    stock: int = Field(ge=0, le=100_000)
    is_active: bool = True
    # Двигатели названиями: отсутствующие создаются, существующие переиспользуются.
    engines: list[str] = Field(default_factory=list, max_length=50)


class InjectorUpdate(BaseModel):
    """Частичное изменение: меняются только присланные поля."""

    oem_number: str | None = Field(default=None, min_length=3, max_length=64)
    title: str | None = Field(default=None, min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    manufacturer_id: int | None = None
    type: InjectorType | None = None
    condition: InjectorCondition | None = None
    price_kopecks: int | None = Field(default=None, ge=0, le=100_000_000)
    stock: int | None = Field(default=None, ge=0, le=100_000)
    is_active: bool | None = None
    engines: list[str] | None = Field(default=None, max_length=50)
