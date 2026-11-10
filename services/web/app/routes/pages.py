"""Главная, страница ремонта, пробы здоровья."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, Response
from redis.exceptions import RedisError

from app.api_client import ApiError, ApiUnavailable
from app.deps import ApiDep
from app.errors import is_htmx
from app.templating import render

router = APIRouter()
health_router = APIRouter(prefix="/health")

REPAIR_PRICES = [
    ("Диагностика форсунок на стенде", "от 1 500 ₽"),
    ("Чистка и проверка (ультразвук)", "от 2 500 ₽"),
    ("Ремонт Common Rail (Bosch, Denso, Delphi, Siemens)", "от 6 000 ₽"),
    ("Ремонт насос-форсунки", "от 5 500 ₽"),
    ("Замена распылителя и клапана", "от 3 500 ₽"),
    ("Кодирование и адаптация (IMA/C2I)", "от 1 000 ₽"),
]


@router.get("/")
async def home(request: Request, api: ApiDep) -> Response:
    featured: list[dict[str, object]] = []
    try:
        page = await api.get("/catalog/injectors", params={"condition": "new", "limit": 4})
        featured = page["items"]
    except (ApiError, ApiUnavailable):
        pass  # витрина не обязательна: главная должна открываться и при сбое каталога
    return render(request, "index.html", {"featured": featured})


@router.get("/repair")
async def repair_page(request: Request) -> Response:
    # Ключ идемпотентности создаётся при показе формы: повторная отправка той же формы (двойной клик,
    # «обновить страницу») не создаст вторую заявку.
    return render(
        request, "repair.html", {"prices": REPAIR_PRICES, "idem": str(uuid.uuid4()), "form": {}}
    )


@router.post("/repair")
async def repair_submit(
    request: Request,
    api: ApiDep,
    name: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "",
    vehicle: Annotated[str, Form()] = "",
    injector: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    website: Annotated[str, Form()] = "",  # поле-ловушка для ботов, человек его не видит
    idem: Annotated[str, Form()] = "",
) -> Response:
    form = {
        "name": name,
        "phone": phone,
        "email": email,
        "vehicle": vehicle,
        "injector": injector,
        "description": description,
    }
    try:
        key = str(uuid.UUID(idem))
    except ValueError:
        key = str(uuid.uuid4())
    payload = {k: (v.strip() or None) for k, v in form.items()}
    payload["website"] = website
    try:
        await api.post("/repair-requests", json=payload, headers={"Idempotency-Key": key})
    except ApiError as exc:
        if exc.status not in (422, 429, 409):
            raise
        context = {
            "prices": REPAIR_PRICES,
            "idem": key,
            "form": form,
            "error": exc.detail if exc.status != 422 else "Проверьте поля формы",
            "field_errors": exc.field_errors,
        }
        status = 429 if exc.status == 429 else 422
        if is_htmx(request):
            return render(request, "partials/_repair_form.html", context, status_code=200)
        return render(request, "repair.html", context, status)
    if is_htmx(request):
        return render(request, "partials/_repair_done.html")
    return render(
        request, "repair.html", {"prices": REPAIR_PRICES, "idem": key, "form": {}, "done": True}
    )


@health_router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@health_router.get("/ready")
async def ready(request: Request) -> Response:
    """Готов, если доступны Redis (сессии) и api. Каждая проверка ограничена по времени."""
    checks: dict[str, str] = {}
    try:
        await request.app.state.redis.ping()
        checks["redis"] = "ok"
    except (RedisError, OSError):
        checks["redis"] = "fail"
    try:
        response = await request.app.state.http.get("/health/live", timeout=2.0)
        checks["api"] = "ok" if response.status_code == 200 else "fail"
    except Exception:
        checks["api"] = "fail"
    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        {"status": "ok" if healthy else "degraded", "checks": checks},
        status_code=200 if healthy else 503,
    )
