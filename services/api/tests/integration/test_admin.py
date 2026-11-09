import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.core.cache import Cache
from tests.integration.conftest import PASSWORD


async def admin_login(
    client: httpx.AsyncClient, email: str, password: str = PASSWORD
) -> httpx.Response:
    return await client.post(
        "/admin/login", data={"username": email, "password": password}, follow_redirects=False
    )


async def test_admin_requires_login(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/", follow_redirects=False)

    assert response.status_code in (302, 307)
    assert response.headers["location"].endswith("/admin/login")


async def test_customer_and_wrong_password_cannot_log_in(
    client: httpx.AsyncClient, customer_headers: dict[str, str], admin_headers: dict[str, str]
) -> None:
    customer = await admin_login(client, "buyer@example.com")
    wrong = await admin_login(client, "admin@example.com", "wrong-password")
    unknown = await admin_login(client, "nobody@example.com")

    for response in (customer, wrong, unknown):
        assert response.status_code == 400  # SQLAdmin показывает форму входа с ошибкой
    assert (await client.get("/admin/", follow_redirects=False)).status_code in (302, 307)


async def test_admin_sees_injector_list_after_login(
    client: httpx.AsyncClient, admin_headers: dict[str, str], catalog: dict[str, int]
) -> None:
    login = await admin_login(client, "admin@example.com")
    assert login.status_code in (302, 303)

    page = await client.get("/admin/injector/list")

    assert page.status_code == 200
    assert "0 445 110 183" in page.text


async def test_demoted_admin_loses_access_immediately(
    app: FastAPI, client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    await admin_login(client, "admin@example.com")
    assert (await client.get("/admin/", follow_redirects=False)).status_code == 200

    async with app.state.resources.engine.begin() as conn:
        await conn.execute(text("UPDATE users SET role = 'customer'"))

    assert (await client.get("/admin/", follow_redirects=False)).status_code in (302, 307)


async def test_change_in_admin_resets_catalog_cache(
    app: FastAPI, client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    await admin_login(client, "admin@example.com")
    cache = Cache(app.state.resources.redis)
    before = await cache.version()

    created = await client.post(
        "/admin/manufacturer/create",
        data={"name": "Cummins", "slug": "cummins"},
        follow_redirects=False,
    )

    assert created.status_code in (302, 303), created.text[:300]
    assert await cache.version() == before + 1
    names = [m["name"] for m in (await client.get("/catalog/manufacturers")).json()]
    assert "Cummins" in names
