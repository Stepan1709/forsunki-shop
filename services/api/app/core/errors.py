"""Единый формат ошибок API: Problem Details (RFC 9457, application/problem+json)."""

from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.correlation import CORRELATION_HEADER, current_correlation_id

PROBLEM_MEDIA_TYPE = "application/problem+json"

log = structlog.get_logger("errors")


class AppError(Exception):
    """Ожидаемая бизнес-ошибка. Сервисы бросают её, а не HTTPException (слои не знают про HTTP)."""

    status_code = 400

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.headers = headers
        if status_code is not None:
            self.status_code = status_code


class UnauthorizedError(AppError):
    status_code = 401

    def __init__(self, detail: str = "Требуется авторизация") -> None:
        super().__init__(detail, headers={"WWW-Authenticate": "Bearer"})


class ForbiddenError(AppError):
    status_code = 403

    def __init__(self, detail: str = "Недостаточно прав") -> None:
        super().__init__(detail)


class NotFoundError(AppError):
    status_code = 404


class ConflictError(AppError):
    status_code = 409


class TooManyRequestsError(AppError):
    status_code = 429

    def __init__(self, retry_after: int) -> None:
        super().__init__(
            "Слишком много попыток, повторите позже", headers={"Retry-After": str(retry_after)}
        )


def problem_response(
    status: int,
    detail: str | None = None,
    *,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
    }
    if detail:
        body["detail"] = detail
    response_headers = dict(headers or {})
    correlation_id = current_correlation_id()
    if correlation_id:
        # По этому id клиент и поддержка находят запрос в логах.
        body["correlation_id"] = correlation_id
        response_headers[CORRELATION_HEADER] = correlation_id
    if extra:
        body.update(extra)
    return JSONResponse(
        body, status_code=status, media_type=PROBLEM_MEDIA_TYPE, headers=response_headers
    )


async def _http_exception(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    return problem_response(exc.status_code, str(exc.detail), headers=dict(exc.headers or {}))


async def _validation_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    # Не возвращаем введённые значения (`input`): там бывают пароли и персональные данные.
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return problem_response(422, "Ошибка валидации входных данных", extra={"errors": errors})


async def _app_error(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return problem_response(exc.status_code, exc.detail, headers=exc.headers)


async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
    # Подробности только в лог. Клиенту их не отдаём: внутренности системы наружу не выходят.
    log.error("unhandled_exception", exc_info=exc)
    return problem_response(500, "Внутренняя ошибка сервера")


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, _http_exception)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(Exception, _unhandled)
