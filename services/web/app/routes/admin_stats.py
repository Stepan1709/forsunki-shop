"""Страница «Статистика» в админке: просмотры, регистрации, покупки за выбранный период и рейтинги товаров.

Все числа считает api (`GET /admin/stats`); здесь выбор периода, группировка и рисование графиков (SVG, см. charts.py).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from app.charts import bar_chart
from app.deps import AdminDep, ApiDep
from app.templating import render

router = APIRouter(prefix="/admin/stats")

_MSK = timezone(timedelta(hours=3))
PRESETS = {"7": "7 дней", "30": "30 дней", "90": "90 дней", "365": "Год"}
GROUPS = {"day": "По дням", "week": "По неделям", "month": "По месяцам"}
MAX_DAYS = 366  # столько же хранится и принимается api
_MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _today() -> date:
    return datetime.now(_MSK).date()


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def resolve_period(period: str, raw_from: str, raw_to: str) -> tuple[date, date, str, str | None]:
    """Период по выбору пользователя: (с, по, выбранный пресет или custom, сообщение об ошибке ввода)."""
    today = _today()
    if period in PRESETS:
        days = int(period)
        return today - timedelta(days=days - 1), today, period, None
    if period == "custom":
        start, end = _parse_date(raw_from), _parse_date(raw_to)
        if start is None or end is None:
            error = "Укажите обе даты периода."
        elif start > end:
            error = "Начало периода позже его конца."
        elif (end - start).days + 1 > MAX_DAYS:
            error = f"Период не может быть длиннее {MAX_DAYS} дней."
        else:
            return start, end, "custom", None
        return today - timedelta(days=29), today, "custom", error
    return today - timedelta(days=29), today, "30", None


def default_group(start: date, end: date) -> str:
    """Чем длиннее период, тем крупнее шаг: иначе на графике сотни неразличимых столбиков."""
    days = (end - start).days + 1
    if days <= 31:
        return "day"
    if days <= 182:
        return "week"
    return "month"


def point_label(start: date, group: str, *, show_year: bool) -> str:
    if group == "month":
        return (
            f"{_MONTHS[start.month - 1]} {start.year % 100:02d}"
            if show_year
            else _MONTHS[start.month - 1]
        )
    return f"{start.day:02d}.{start.month:02d}"


def build_charts(report: dict[str, Any]) -> dict[str, str]:
    points = report["points"]
    group = report["group"]
    first = date.fromisoformat(points[0]["start"]) if points else None
    last = date.fromisoformat(points[-1]["start"]) if points else None
    show_year = bool(first and last and first.year != last.year)
    labels = [
        point_label(date.fromisoformat(p["start"]), group, show_year=show_year) for p in points
    ]
    return {
        "page_views": bar_chart(
            labels, [p["page_views"] for p in points], title="Просмотры страниц", unit="просм."
        ),
        "registrations": bar_chart(
            labels, [p["registrations"] for p in points], title="Новые регистрации", unit="чел."
        ),
        "orders": bar_chart(
            labels, [p["orders"] for p in points], title="Оплаченные заказы", unit="шт."
        ),
        "revenue": bar_chart(
            labels,
            [p["revenue_kopecks"] for p in points],
            title="Выручка по оплаченным заказам",
            value_format="money",
        ),
    }


@router.get("")
async def stats_page(
    request: Request,
    api: ApiDep,
    _admin: AdminDep,
    period: str = "30",
    date_from: str = Query("", alias="from"),
    date_to: str = Query("", alias="to"),
    group: str = "",
) -> Response:
    start, end, selected, error = resolve_period(period, date_from, date_to)
    if error:
        request.state.session.flash(error, "error")
    chosen_group = group if group in GROUPS else default_group(start, end)
    report = await api.get(
        "/admin/stats",
        auth=True,
        params={"from": start.isoformat(), "to": end.isoformat(), "group": chosen_group, "top": 10},
    )
    context = {
        "report": report,
        "charts": build_charts(report),
        "presets": PRESETS,
        "groups": GROUPS,
        "period": selected,
        "group": chosen_group,
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
    }
    return render(request, "admin/stats.html", context)
