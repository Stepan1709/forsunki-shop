"""Обработка ошибок: человеческие страницы вместо трассировок.

Для запросов HTMX (частичные обновления страницы) ошибка показывается всплывающим сообщением в #toast:
ответ приходит со статусом 200 и заголовками HX-Retarget, потому что htmx по умолчанию не подставляет в страницу
ответы 4xx и 5xx.
"""

from urllib.parse import quote, urlsplit

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api_client import ApiError, ApiUnavailable, AuthRequired
from app.deps import CsrfFailed, Forbidden
from app.templating import render

log = structlog.get_logger("errors")


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _page(request: Request, status: int, title: str, text: str) -> Response:
    if is_htmx(request):
        response = render(request, "partials/_alert.html", {"text": text, "kind": "error"})
        response.headers["HX-Retarget"] = "#toast"
        response.headers["HX-Reswap"] = "innerHTML"
        return response
    return render(request, "error.html", {"title": title, "text": text, "code": status}, status)


async def _auth_required(request: Request, _exc: Exception) -> Response:
    # Возвращаемся на ту же страницу после входа. Берём только путь с параметрами: адрес сайта из заголовка
    # отбрасывается, так что вести на чужой домен ссылка не может.
    current = urlsplit(request.headers.get("HX-Current-URL") or str(request.url))
    path = current.path + (f"?{current.query}" if current.query else "")
    login = f"/login?next={quote(path)}"
    if is_htmx(request):
        return Response(status_code=204, headers={"HX-Redirect": login})
    return RedirectResponse(login, status_code=303)


async def _forbidden(request: Request, _exc: Exception) -> Response:
    return _page(request, 403, "Нет доступа", "У вашей учётной записи нет прав на эту страницу.")


async def _csrf(request: Request, _exc: Exception) -> Response:
    log.warning("csrf_rejected", path=request.url.path)
    return _page(
        request,
        403,
        "Сессия устарела",
        "Обновите страницу и повторите действие: проверочный токен не совпал.",
    )


async def _unavailable(request: Request, _exc: Exception) -> Response:
    return _page(
        request, 503, "Сервис временно недоступен", "Магазин не отвечает. Попробуйте через минуту."
    )


async def _api_error(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, ApiError):
        raise exc
    if exc.status == 404:
        return _page(request, 404, "Не найдено", "Такой страницы или товара нет.")
    if exc.status == 429:
        return _page(request, 429, "Слишком много запросов", exc.detail)
    if exc.status in (400, 409, 422):
        return _page(request, exc.status, "Не получилось", exc.detail)
    log.error("api_error_unhandled", status=exc.status, detail=exc.detail)
    return _page(
        request, 502, "Что-то пошло не так", "Не удалось выполнить действие. Попробуйте позже."
    )


async def _http_exception(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    if exc.status_code == 404:
        return _page(
            request, 404, "Страница не найдена", "Проверьте адрес или вернитесь на главную."
        )
    if exc.status_code == 405:
        return _page(
            request, 405, "Недопустимое действие", "Эта страница не принимает такой запрос."
        )
    return _page(request, exc.status_code, "Ошибка", str(exc.detail))


async def _unhandled(request: Request, exc: Exception) -> Response:
    log.error("unhandled_exception", exc_info=exc)
    return _page(request, 500, "Внутренняя ошибка", "Мы уже знаем о проблеме. Попробуйте позже.")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AuthRequired, _auth_required)
    app.add_exception_handler(Forbidden, _forbidden)
    app.add_exception_handler(CsrfFailed, _csrf)
    app.add_exception_handler(ApiUnavailable, _unavailable)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(Exception, _unhandled)


__all__ = ["HTMLResponse", "install_error_handlers", "is_htmx"]
