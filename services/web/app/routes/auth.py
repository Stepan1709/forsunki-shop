"""Вход, регистрация, выход."""

from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.api_client import ApiError
from app.deps import ApiDep, MaybeUserDep, SessionDep
from app.templating import render

router = APIRouter()


def safe_next(value: str | None) -> str:
    """Только путь внутри сайта: «//evil.com» и «https://...» превратили бы вход в открытое перенаправление."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def _error_text(exc: ApiError) -> str:
    if exc.status == 429:
        wait = f" Повторите через {exc.retry_after} с." if exc.retry_after else ""
        return f"Слишком много попыток.{wait}"
    return exc.detail


@router.get("/login")
async def login_page(request: Request, user: MaybeUserDep, next: str = "/") -> Response:
    if user:
        return RedirectResponse(safe_next(next), status_code=303)
    return render(request, "auth/login.html", {"next": safe_next(next)})


@router.post("/login")
async def login(
    request: Request,
    session: SessionDep,
    api: ApiDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    try:
        await api.login(email.strip(), password)
    except ApiError as exc:
        status = 429 if exc.status == 429 else 400
        return render(
            request,
            "auth/login.html",
            {"next": safe_next(next), "error": _error_text(exc), "email": email},
            status,
        )
    session.rotate()  # новый id сессии после входа: защита от фиксации сессии
    return RedirectResponse(safe_next(next), status_code=303)


@router.get("/register")
async def register_page(request: Request, user: MaybeUserDep) -> Response:
    if user:
        return RedirectResponse("/", status_code=303)
    return render(request, "auth/register.html")


@router.post("/register")
async def register(
    request: Request,
    session: SessionDep,
    api: ApiDep,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    password2: Annotated[str, Form()],
) -> Response:
    def failed(error: str, fields: dict[str, str] | None = None, status: int = 400) -> Response:
        return render(
            request,
            "auth/register.html",
            {"error": error, "field_errors": fields or {}, "email": email},
            status,
        )

    if password != password2:
        return failed("Пароли не совпадают")
    try:
        await api.post("/auth/register", json={"email": email.strip(), "password": password})
        await api.login(email.strip(), password)
    except ApiError as exc:
        fields = {k: v for k, v in exc.field_errors.items() if k in ("email", "password")}
        return failed(
            _error_text(exc) if not fields else "Проверьте поля формы",
            fields,
            429 if exc.status == 429 else 400,
        )
    session.rotate()
    session.flash("Добро пожаловать! Вы зарегистрированы.", "success")
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(session: SessionDep, api: ApiDep) -> Response:
    await api.logout()
    session.destroy()
    return RedirectResponse("/", status_code=303)
