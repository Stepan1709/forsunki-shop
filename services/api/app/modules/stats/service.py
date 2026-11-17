"""Отчёт для страницы «Статистика»: просмотры из дневных итогов, регистрации и покупки из users и orders."""

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import Date, DateTime, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.modules.catalog.models import Injector
from app.modules.orders.models import Order, OrderItem
from app.modules.orders.state import OrderStatus
from app.modules.stats.models import ProductViewsDaily, SiteStatsDaily
from app.modules.stats.schemas import (
    Group,
    PeriodPoint,
    StatsReport,
    TopPurchased,
    TopViewed,
    Totals,
)
from app.modules.users.models import User

# Магазин работает по московскому времени и дни считает по нему: заказ, оплаченный в 23:30 по Москве, относится
# к этому дню, а не к следующему по UTC.
MSK = timezone(timedelta(hours=3))
MSK_NAME = "Europe/Moscow"

# Просмотры хранятся год (retention_stats_days), поэтому и запрашивать за больший срок нечего.
MAX_RANGE_DAYS = 366


def today() -> date:
    return datetime.now(MSK).date()


def bucket_start(day: date, group: Group) -> date:
    """Начало периода, в который попадает день: сам день, понедельник недели или первое число месяца."""
    if group == "day":
        return day
    if group == "week":
        return day - timedelta(days=day.weekday())
    return day.replace(day=1)


def next_bucket(start: date, group: Group) -> date:
    if group == "day":
        return start + timedelta(days=1)
    if group == "week":
        return start + timedelta(days=7)
    return (start.replace(day=28) + timedelta(days=4)).replace(day=1)


def validate_range(date_from: date | None, date_to: date | None) -> tuple[date, date]:
    """По умолчанию последние 30 дней, включая сегодня."""
    end = date_to or today()
    start = date_from or end - timedelta(days=29)
    if start > end:
        raise AppError("Начало периода позже его конца", status_code=422)
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise AppError(f"Период не больше {MAX_RANGE_DAYS} дней", status_code=422)
    return start, end


class StatsService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def report(
        self, date_from: date | None, date_to: date | None, group: Group, top: int
    ) -> StatsReport:
        start, end = validate_range(date_from, date_to)

        # Границы по московскому времени без часового пояса: сравниваем с «локальным» временем строк.
        lower = datetime.combine(start, time.min)
        upper = datetime.combine(end + timedelta(days=1), time.min)

        views = await self._page_views(start, end, group)
        registrations = await self._registrations(lower, upper, group)
        orders = await self._orders(lower, upper, group)

        points: list[PeriodPoint] = []
        cursor = bucket_start(start, group)
        while cursor <= end:
            order_count, revenue = orders.get(cursor, (0, 0))
            points.append(
                PeriodPoint(
                    start=cursor,
                    page_views=views.get(cursor, 0),
                    registrations=registrations.get(cursor, 0),
                    orders=order_count,
                    revenue_kopecks=revenue,
                )
            )
            cursor = next_bucket(cursor, group)

        product_views = await self._product_views_total(start, end)
        totals = Totals(
            page_views=sum(p.page_views for p in points),
            product_views=product_views,
            registrations=sum(p.registrations for p in points),
            orders=sum(p.orders for p in points),
            revenue_kopecks=sum(p.revenue_kopecks for p in points),
        )
        return StatsReport(
            date_from=start,
            date_to=end,
            group=group,
            views_tracked_since=await self._tracked_since(),
            totals=totals,
            points=points,
            top_viewed=await self._top_viewed(start, end, top),
            top_purchased=await self._top_purchased(lower, upper, top),
        )

    # ------------------------------------------------------------------------------------ запросы
    async def _page_views(self, start: date, end: date, group: Group) -> dict[date, int]:
        bucket = cast(func.date_trunc(group, cast(SiteStatsDaily.day, DateTime)), Date)
        rows = await self._session.execute(
            select(bucket, func.sum(SiteStatsDaily.page_views))
            .where(SiteStatsDaily.day.between(start, end))
            .group_by(bucket)
        )
        return {row[0]: int(row[1]) for row in rows}

    async def _registrations(
        self, lower: datetime, upper: datetime, group: Group
    ) -> dict[date, int]:
        local = func.timezone(MSK_NAME, User.created_at)
        bucket = cast(func.date_trunc(group, local), Date)
        rows = await self._session.execute(
            select(bucket, func.count()).where(local >= lower, local < upper).group_by(bucket)
        )
        return {row[0]: int(row[1]) for row in rows}

    async def _orders(
        self, lower: datetime, upper: datetime, group: Group
    ) -> dict[date, tuple[int, int]]:
        """Оплаченные заказы по дате оплаты (а не оформления): деньги относятся ко дню, когда они пришли."""
        local = func.timezone(MSK_NAME, Order.paid_at)
        bucket = cast(func.date_trunc(group, local), Date)
        rows = await self._session.execute(
            select(bucket, func.count(), func.coalesce(func.sum(Order.total_kopecks), 0))
            .where(Order.status == OrderStatus.PAID, local >= lower, local < upper)
            .group_by(bucket)
        )
        return {row[0]: (int(row[1]), int(row[2])) for row in rows}

    async def _product_views_total(self, start: date, end: date) -> int:
        total = await self._session.scalar(
            select(func.coalesce(func.sum(ProductViewsDaily.views), 0)).where(
                ProductViewsDaily.day.between(start, end)
            )
        )
        return int(total or 0)

    async def _tracked_since(self) -> date | None:
        return await self._session.scalar(select(func.min(SiteStatsDaily.day)))

    async def _top_viewed(self, start: date, end: date, top: int) -> list[TopViewed]:
        views = func.sum(ProductViewsDaily.views).label("views")
        rows = await self._session.execute(
            select(Injector.id, Injector.oem_number, Injector.title, views)
            .join(Injector, Injector.id == ProductViewsDaily.injector_id)
            .where(ProductViewsDaily.day.between(start, end))
            .group_by(Injector.id, Injector.oem_number, Injector.title)
            .order_by(views.desc(), Injector.id)
            .limit(top)
        )
        return [
            TopViewed(injector_id=r[0], oem_number=r[1], title=r[2], views=int(r[3])) for r in rows
        ]

    async def _top_purchased(
        self, lower: datetime, upper: datetime, top: int
    ) -> list[TopPurchased]:
        """Покупки считаются по строкам оплаченных заказов; название и артикул берём из снимка в заказе."""
        local = func.timezone(MSK_NAME, Order.paid_at)
        quantity = func.sum(OrderItem.quantity).label("quantity")
        revenue = func.sum(OrderItem.quantity * OrderItem.unit_price_kopecks)
        rows = await self._session.execute(
            select(
                OrderItem.injector_id,
                func.max(OrderItem.oem_number),
                func.max(OrderItem.title),
                quantity,
                revenue,
            )
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.status == OrderStatus.PAID, local >= lower, local < upper)
            .group_by(OrderItem.injector_id)
            .order_by(quantity.desc(), OrderItem.injector_id)
            .limit(top)
        )
        return [
            TopPurchased(
                injector_id=r[0],
                oem_number=r[1],
                title=r[2],
                quantity=int(r[3]),
                revenue_kopecks=int(r[4]),
            )
            for r in rows
        ]
