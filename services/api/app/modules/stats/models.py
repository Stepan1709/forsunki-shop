"""Дневные итоги статистики: просмотры страниц сайта и просмотры карточек товаров.

Просмотры считает сайт (`web`) в Redis, воркер переносит накопленное сюда (см. `worker/stats_flush.py`).
Регистрации и покупки отдельно не хранятся: они считаются из таблиц users и orders. Просмотры за прошлое
восстановить нельзя, поэтому отсчёт идёт с момента включения подсчёта.
"""

from datetime import date

from sqlalchemy import BigInteger, Date, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SiteStatsDaily(Base):
    """Сколько страниц сайта открыли за день (все страницы, без различия посетителей)."""

    __tablename__ = "site_stats_daily"

    # День по московскому времени (так его пишет сайт): магазин работает по московскому графику.
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    page_views: Mapped[int] = mapped_column(BigInteger, default=0)


class ProductViewsDaily(Base):
    """Сколько раз за день открывали карточку конкретной форсунки."""

    __tablename__ = "product_views_daily"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    # Товар удаляют редко (обычно скрывают), но при удалении его просмотры тоже уходят.
    injector_id: Mapped[int] = mapped_column(
        ForeignKey("injectors.id", ondelete="CASCADE"), primary_key=True
    )
    views: Mapped[int] = mapped_column(BigInteger, default=0)

    # Топ товаров за период группирует по товару; индекс также ускоряет каскадное удаление при удалении товара.
    __table_args__ = (Index("ix_product_views_daily_injector_id", "injector_id"),)
