"""Адрес клиента за nginx: сайт запоминает его и передаёт в api заголовком X-Forwarded-For."""

import httpx
from fastapi import FastAPI

from app.clientip import ClientIpMiddleware, current_client_ip


def make_app(trusted: list[str]) -> FastAPI:
    app = FastAPI()
    app.add_middleware(ClientIpMiddleware, trusted_proxies=trusted)

    @app.get("/ip")
    async def ip() -> dict[str, str | None]:
        return {"ip": current_client_ip()}

    return app


async def get_ip(app: FastAPI, headers: dict[str, str], client: tuple[str, int]) -> str | None:
    transport = httpx.ASGITransport(app=app, client=client)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        value: str | None = (await http.get("/ip", headers=headers)).json()["ip"]
    return value


async def test_client_ip_from_forwarded_header_of_trusted_proxy() -> None:
    app = make_app(["172.16.0.0/12"])
    # Левая запись подделана клиентом, правая добавлена nginx: верим только правой.
    got = await get_ip(app, {"X-Forwarded-For": "6.6.6.6, 198.51.100.9"}, ("172.18.0.4", 5000))
    assert got == "198.51.100.9"


async def test_forwarded_header_from_untrusted_peer_is_ignored() -> None:
    app = make_app(["172.16.0.0/12"])
    got = await get_ip(app, {"X-Forwarded-For": "6.6.6.6"}, ("203.0.113.7", 5000))
    assert got == "203.0.113.7"
