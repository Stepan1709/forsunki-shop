from datetime import date
from typing import Literal

from pydantic import BaseModel

Group = Literal["day", "week", "month"]


class PeriodPoint(BaseModel):
    """Одна точка графика: начало периода (день, понедельник недели или первое число месяца) и числа."""

    start: date
    page_views: int
    registrations: int
    orders: int  # оплаченные заказы
    revenue_kopecks: int  # сумма оплаченных заказов


class Totals(BaseModel):
    page_views: int
    product_views: int
    registrations: int
    orders: int
    revenue_kopecks: int


class TopViewed(BaseModel):
    injector_id: int
    oem_number: str
    title: str
    views: int


class TopPurchased(BaseModel):
    injector_id: int
    oem_number: str
    title: str
    quantity: int
    revenue_kopecks: int


class StatsReport(BaseModel):
    date_from: date
    date_to: date
    group: Group
    # С какого дня считаются просмотры: до этой даты данных нет (счётчики включили позже). None, если ещё
    # ни одного просмотра не записано.
    views_tracked_since: date | None
    totals: Totals
    points: list[PeriodPoint]
    top_viewed: list[TopViewed]
    top_purchased: list[TopPurchased]
