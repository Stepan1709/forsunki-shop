"""Содержимое страницы «О нас»: два текстовых блока и список частых вопросов.

Тексты и вопросы лежат в БД, а не в шаблонах сайта: администратор правит их без выкатки новой версии.
"""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func, true
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base

# Какие блоки текста есть на странице. Список закрыт: произвольные slug'и админ создать не может.
PAGE_SHOP = "about_shop"
PAGE_REPAIR = "about_repair"
PAGE_SLUGS = (PAGE_SHOP, PAGE_REPAIR)


class SitePage(Base):
    """Редактируемый текстовый блок. Одна строка на блок; если строки нет, показывается текст по умолчанию."""

    __tablename__ = "site_pages"

    slug: Mapped[str] = mapped_column(String(50), primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # Обычный текст: пустая строка между абзацами разделяет абзацы. HTML не поддерживается, поэтому
    # администратор (или тот, кто украл его сессию) не может вставить на страницу скрипт.
    body: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class FaqItem(Base):
    __tablename__ = "faq_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    question: Mapped[str] = mapped_column(String(300))
    answer: Mapped[str] = mapped_column(Text)
    # Порядок показа: по возрастанию. Новый вопрос получает максимум + 1; перемещение меняет позиции местами.
    position: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_faq_items_position", "position"),)
