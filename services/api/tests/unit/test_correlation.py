import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import Settings
from app.core.correlation import CORRELATION_HEADER
from app.main import create_app


@asynccontextmanager
async def no_resources(_app: FastAPI) -> AsyncIterator[None]:
    yield


def build_app(settings: Settings) -> FastAPI:
    app = create_app(settings, lifespan=no_resources)

    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    app.add_api_route("/ping", ping)
    return app


async def get(app: FastAPI, headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/ping", headers=headers)


async def test_generates_id_when_missing(settings: Settings) -> None:
    response = await get(build_app(settings))

    assert len(response.headers[CORRELATION_HEADER]) == 32


async def test_propagates_incoming_id(settings: Settings) -> None:
    response = await get(build_app(settings), {CORRELATION_HEADER: "web-123.abc"})

    assert response.headers[CORRELATION_HEADER] == "web-123.abc"


async def test_replaces_unsafe_id(settings: Settings) -> None:
    response = await get(build_app(settings), {CORRELATION_HEADER: "bad id; drop"})

    assert response.headers[CORRELATION_HEADER] != "bad id; drop"


async def test_request_is_logged_as_json_with_required_fields(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    app = build_app(settings)

    await get(app, {CORRELATION_HEADER: "cid-1"})

    records = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    record = next(r for r in records if r["event"] == "http_request")
    assert record["correlation_id"] == "cid-1"
    assert record["service"] == "api"
    assert record["env"] == "test"
    assert record["level"] == "info"
    assert record["status"] == 200
    assert "timestamp" in record
