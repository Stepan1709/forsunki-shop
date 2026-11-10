"""Сессии, CSRF, вход и выход."""

import httpx
from fastapi import FastAPI

from app.routes.auth import safe_next
from tests.conftest import csrf_of, get_token, login
from tests.fake_api import FakeApi


async def test_session_cookie_is_httponly_and_holds_no_tokens(
    client: httpx.AsyncClient, api: FakeApi
) -> None:
    response = await login(client)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    sid = client.cookies["sid"]
    # В cookie только случайный id: ни токенов, ни почты.
    assert "access" not in sid.lower()
    assert "buyer" not in sid
    assert len(sid) >= 40


async def test_tokens_live_in_redis_not_in_browser(client: httpx.AsyncClient, app: FastAPI) -> None:
    await login(client)
    keys = await app.state.redis.keys("web:sess:*")
    assert keys
    stored = [await app.state.redis.get(k) for k in keys]
    assert any("access-" in value and "refresh-" in value for value in stored)
    # Ключ строится из хэша id, а не из самого значения cookie.
    assert all(client.cookies["sid"] not in key for key in keys)


async def test_login_rotates_session_id(client: httpx.AsyncClient) -> None:
    await client.get("/login")
    before = client.cookies["sid"]
    await login(client)
    assert client.cookies["sid"] != before  # защита от фиксации сессии


async def test_post_without_csrf_token_is_rejected(client: httpx.AsyncClient, api: FakeApi) -> None:
    await client.get("/login")
    response = await client.post(
        "/login", data={"email": "buyer@example.com", "password": "pass-12345"}
    )
    assert response.status_code == 403
    wrong = await client.post(
        "/login",
        data={"email": "buyer@example.com", "password": "pass-12345", "csrf_token": "x" * 40},
    )
    assert wrong.status_code == 403
    assert api.called("POST", "/auth/login") == []  # до api запрос не дошёл


async def test_csrf_token_in_header_is_accepted(client: httpx.AsyncClient) -> None:
    token = await get_token(client)
    response = await client.post(
        "/login",
        data={"email": "buyer@example.com", "password": "pass-12345"},
        headers={"X-CSRF-Token": token},
    )
    assert response.status_code == 303


async def test_csrf_token_of_another_session_is_rejected(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)
    async with (
        httpx.AsyncClient(transport=transport, base_url="http://test") as a,
        httpx.AsyncClient(transport=transport, base_url="http://test") as b,
    ):
        token_of_a = csrf_of((await a.get("/login")).text)
        await b.get("/login")
        response = await b.post(
            "/login",
            data={"email": "buyer@example.com", "password": "pass-12345", "csrf_token": token_of_a},
        )
        assert response.status_code == 403


async def test_wrong_password_shows_message_and_stays_anonymous(client: httpx.AsyncClient) -> None:
    response = await login(client, password="wrong-password")
    assert response.status_code == 400
    assert "Неверный email или пароль" in response.text
    assert (await client.get("/profile")).status_code == 303


async def test_protected_pages_redirect_to_login_with_return_path(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/cart")
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=/cart"


async def test_login_redirects_back_to_safe_path_only(client: httpx.AsyncClient) -> None:
    token = await get_token(client)
    response = await client.post(
        "/login",
        data={
            "email": "buyer@example.com",
            "password": "pass-12345",
            "csrf_token": token,
            "next": "//evil.com",
        },
    )
    assert response.headers["location"] == "/"


def test_safe_next() -> None:
    assert safe_next("/shop?q=1") == "/shop?q=1"
    for bad in ("//evil.com", "https://evil.com", "evil", "/\\evil.com", "", None):
        assert safe_next(bad) == "/"


async def test_logout_destroys_session_and_revokes_tokens(
    buyer: httpx.AsyncClient, api: FakeApi, app: FastAPI
) -> None:
    token = csrf_of((await buyer.get("/")).text)
    response = await buyer.post("/logout", data={"csrf_token": token})
    assert response.status_code == 303
    assert api.called("POST", "/auth/logout")  # refresh-токен отозван в api
    assert await app.state.redis.keys("web:sess:*") == []
    assert (await buyer.get("/profile")).status_code == 303


async def test_register_logs_in_and_validates(client: httpx.AsyncClient) -> None:
    token = await get_token(client)
    mismatch = await client.post(
        "/register",
        data={
            "email": "n@example.com",
            "password": "pass-12345",
            "password2": "other",
            "csrf_token": token,
        },
    )
    assert mismatch.status_code == 400
    assert "не совпадают" in mismatch.text

    short = await client.post(
        "/register",
        data={
            "email": "n@example.com",
            "password": "short",
            "password2": "short",
            "csrf_token": token,
        },
    )
    assert short.status_code == 400
    assert "слишком короткий" in short.text

    ok = await client.post(
        "/register",
        data={
            "email": "new@example.com",
            "password": "pass-12345",
            "password2": "pass-12345",
            "csrf_token": token,
        },
    )
    assert ok.status_code == 303
    assert (await client.get("/profile")).status_code == 200


async def test_correlation_id_is_sent_to_api(buyer: httpx.AsyncClient, api: FakeApi) -> None:
    await buyer.get("/cart", headers={"X-Request-ID": "trace-abc-123"})
    cart_calls = api.called("GET", "/cart")
    assert cart_calls[-1].headers["x-request-id"] == "trace-abc-123"
