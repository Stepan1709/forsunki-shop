"""Jinja2: окружение, фильтры и общий контекст страниц."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings
from app.deps import CurrentUser, current_user
from app.sessions import Session

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")

_MSK = ZoneInfo("Europe/Moscow")
NBSP = " "

STATUS_LABELS = {
    "PENDING_PAYMENT": ("Ожидает оплаты", "amber"),
    "PAID": ("Оплачен", "green"),
    "PAYMENT_FAILED": ("Платёж отклонён", "red"),
    "CANCELLED": ("Отменён", "gray"),
    "EXPIRED": ("Не оплачен вовремя", "gray"),
}
CONDITION_LABELS = {"new": "Новая", "remanufactured": "Восстановленная"}
TYPE_LABELS = {"common_rail": "Common Rail", "pump_injector": "Насос-форсунка"}


def money(kopecks: int) -> str:
    rubles, kop = divmod(int(kopecks), 100)
    return f"{rubles:,}".replace(",", NBSP) + f",{kop:02d}{NBSP}₽"


def dt(value: str | datetime) -> str:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.astimezone(_MSK).strftime("%d.%m.%Y %H:%M")


def rubles_input(kopecks: int) -> str:
    """Цена для поля формы: 4500000 -> '45000.00'."""
    return f"{Decimal(int(kopecks)) / 100:.2f}"


def parse_rubles(text: str) -> int | None:
    """'45 000,50' -> 4500050. Деньги считаем Decimal, а не float; неверный ввод даёт None."""
    cleaned = text.replace(NBSP, "").replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    if value < 0 or value != value.quantize(Decimal("0.01")):
        return None
    return int(value * 100)


templates.env.filters["money"] = money
templates.env.filters["dt"] = dt
templates.env.globals.update(
    status_labels=STATUS_LABELS,
    condition_labels=CONDITION_LABELS,
    type_labels=TYPE_LABELS,
    rubles_input=rubles_input,
)


def render(
    request: Request, name: str, context: dict[str, Any] | None = None, status_code: int = 200
) -> HTMLResponse:
    """Страница с общим контекстом: пользователь, CSRF-токен, всплывающие сообщения, настройки магазина."""
    session: Session = request.state.session
    settings: Settings = request.app.state.settings
    user: CurrentUser | None = current_user(session)
    full: dict[str, Any] = {
        "request": request,
        "user": user,
        "csrf_token": session.csrf_token,
        "flash": session.pop_flash(),
        "shop": settings,
        **(context or {}),
    }
    return templates.TemplateResponse(request, name, full, status_code=status_code)
