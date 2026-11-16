from typing import Literal

from pydantic import BaseModel, Field


class PageOut(BaseModel):
    slug: str
    title: str
    body: str


class FaqOut(BaseModel):
    id: int
    question: str
    answer: str


class AboutOut(BaseModel):
    """Всё, что нужно странице «О нас», одним ответом."""

    shop: PageOut
    repair: PageOut
    faq: list[FaqOut]


# ----------------------------------------------------------------------------------- администратор
class FaqAdminOut(FaqOut):
    position: int
    is_active: bool


class AboutAdminOut(BaseModel):
    pages: list[PageOut]
    faq: list[FaqAdminOut]  # и скрытые вопросы тоже


class PageUpdate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    body: str = Field(min_length=1, max_length=10_000)


class FaqCreate(BaseModel):
    question: str = Field(min_length=3, max_length=300)
    answer: str = Field(min_length=1, max_length=5_000)
    is_active: bool = True


class FaqUpdate(BaseModel):
    """Частичное изменение: меняются только присланные поля."""

    question: str | None = Field(default=None, min_length=3, max_length=300)
    answer: str | None = Field(default=None, min_length=1, max_length=5_000)
    is_active: bool | None = None


MoveDirection = Literal["up", "down"]
