from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI

from app.core.config import Settings
from app.core.correlation import CORRELATION_HEADER
from app.core.errors import AppError
from app.main import create_app


@asynccontextmanager
async def no_resources(_app: FastAPI) -> AsyncIterator[None]:
    yield


async def boom() -> None:
    raise RuntimeError("secret internal detail")


async def conflict() -> None:
    raise AppError("Товар закончился", status_code=409)


async def needs_int(x: int) -> dict[str, int]:
    return {"x": x}


def client_for(settings: Settings) -> httpx.AsyncClient:
    app = create_app(settings, lifespan=no_resources)
    app.add_api_route("/boom", boom)
    app.add_api_route("/conflict", conflict)
    app.add_api_route("/needs-int", needs_int)
    # raise_app_exceptions=False: тест видит ответ 500 так, как его увидел бы клиент.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_not_found_is_problem_details(settings: Settings) -> None:
    async with client_for(settings) as client:
        response = await client.get("/nope")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["correlation_id"] == response.headers[CORRELATION_HEADER]


async def test_validation_error_does_not_echo_input(settings: Settings) -> None:
    async with client_for(settings) as client:
        response = await client.get("/needs-int", params={"x": "super-secret-value"})

    assert response.status_code == 422
    assert "super-secret-value" not in response.text
    assert response.json()["errors"][0]["loc"] == ["query", "x"]


async def test_app_error_uses_its_status(settings: Settings) -> None:
    async with client_for(settings) as client:
        response = await client.get("/conflict")

    assert response.status_code == 409
    assert response.json()["detail"] == "Товар закончился"


async def test_unhandled_exception_is_hidden_from_client(settings: Settings) -> None:
    async with client_for(settings) as client:
        response = await client.get("/boom")

    assert response.status_code == 500
    assert "secret internal detail" not in response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert CORRELATION_HEADER in response.headers
