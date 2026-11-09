import re
import uuid
from typing import Self

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator

_PHONE = re.compile(r"[0-9+()\- ]{6,32}")


class RepairRequestIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    phone: str | None = Field(default=None, max_length=32)
    email: EmailStr | None = None
    vehicle: str = Field(min_length=2, max_length=200, description="Марка, модель, двигатель")
    injector: str | None = Field(
        default=None, max_length=200, description="Артикул или производитель форсунки"
    )
    description: str = Field(min_length=10, max_length=2000)
    # Поле-ловушка для ботов: в форме оно скрыто от людей, а бот заполняет все поля подряд.
    website: str = Field(default="", max_length=200)

    @field_validator("name", "vehicle", "description", "injector", "phone", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("phone")
    @classmethod
    def _phone_format(cls, value: str | None) -> str | None:
        if value is not None and not _PHONE.fullmatch(value):
            raise ValueError("Телефон: цифры, пробелы, +, -, скобки; от 6 символов")
        return value

    @model_validator(mode="after")
    def _has_contact(self) -> Self:
        if not self.phone and not self.email:
            raise ValueError("Укажите телефон или email, чтобы мы могли связаться с вами")
        return self


class RepairRequestOut(BaseModel):
    id: uuid.UUID
    status: str
