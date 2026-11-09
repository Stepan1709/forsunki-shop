import asyncio
from typing import Any

import httpx
from fastapi import Depends, FastAPI
from sqlalchemy import text

from app.core.events import get_event_sink
from app.modules.users.deps import require_admin
from app.modules.users.models import User

PASSWORD = "correct-horse-battery"


class RecordingSink:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, routing_key: str, payload: dict[str, Any]) -> None:
        self.events.append((routing_key, payload))


async def register(
    client: httpx.AsyncClient, email: str = "ivan@example.com", password: str = PASSWORD
) -> httpx.Response:
    return await client.post("/auth/register", json={"email": email, "password": password})


async def login(
    client: httpx.AsyncClient, email: str = "ivan@example.com", password: str = PASSWORD
) -> httpx.Response:
    return await client.post("/auth/login", json={"email": email, "password": password})


async def tokens(client: httpx.AsyncClient) -> dict[str, Any]:
    await register(client)
    response = await login(client)
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


# --- Регистрация ---


async def test_register_creates_user_without_exposing_password(client: httpx.AsyncClient) -> None:
    response = await register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "ivan@example.com"
    assert body["role"] == "customer"
    assert "password" not in response.text
    assert "hash" not in response.text


async def test_password_is_stored_only_as_argon2_hash(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await register(client)

    async with app.state.resources.engine.connect() as conn:
        stored = (await conn.execute(text("SELECT password_hash FROM users"))).scalar_one()

    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


async def test_duplicate_email_is_rejected_case_insensitively(client: httpx.AsyncClient) -> None:
    await register(client, "ivan@example.com")

    response = await register(client, "IVAN@Example.com")

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"


async def test_parallel_registration_with_same_email_creates_one_user(
    client: httpx.AsyncClient,
) -> None:
    responses = await asyncio.gather(*(register(client, "race@example.com") for _ in range(4)))

    assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]


async def test_weak_password_and_bad_email_are_rejected(client: httpx.AsyncClient) -> None:
    assert (await register(client, password="short")).status_code == 422
    assert (await register(client, email="not-an-email")).status_code == 422


async def test_registration_publishes_user_registered_once(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    sink = RecordingSink()
    app.dependency_overrides[get_event_sink] = lambda: sink

    await register(client)
    await register(client)  # дубль события не создаёт

    assert [key for key, _ in sink.events] == ["user.registered"]
    assert sink.events[0][1]["email"] == "ivan@example.com"
    app.dependency_overrides.clear()


# --- Вход ---


async def test_login_returns_token_pair(client: httpx.AsyncClient) -> None:
    await register(client)

    response = await login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 900
    assert body["access_token"]
    assert body["refresh_token"]


async def test_wrong_password_and_unknown_email_look_identical(
    client: httpx.AsyncClient,
) -> None:
    await register(client)

    wrong_password = await login(client, password="wrong-password")
    unknown_email = await login(client, email="nobody@example.com")

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["detail"] == unknown_email.json()["detail"]
    assert wrong_password.headers["www-authenticate"] == "Bearer"


async def test_refresh_token_is_stored_as_hash(app: FastAPI, client: httpx.AsyncClient) -> None:
    pair = await tokens(client)

    async with app.state.resources.engine.connect() as conn:
        stored = (await conn.execute(text("SELECT token_hash FROM refresh_tokens"))).scalar_one()

    assert stored != pair["refresh_token"]
    assert len(stored) == 64


# --- Текущий пользователь и роли ---


async def test_me_requires_valid_access_token(client: httpx.AsyncClient) -> None:
    pair = await tokens(client)

    ok = await client.get("/users/me", headers={"Authorization": f"Bearer {pair['access_token']}"})
    anonymous = await client.get("/users/me")
    garbage = await client.get("/users/me", headers={"Authorization": "Bearer garbage"})

    assert ok.status_code == 200
    assert ok.json()["email"] == "ivan@example.com"
    assert anonymous.status_code == 401
    assert garbage.status_code == 401


async def test_refresh_token_cannot_be_used_as_access_token(client: httpx.AsyncClient) -> None:
    pair = await tokens(client)

    response = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {pair['refresh_token']}"}
    )

    assert response.status_code == 401


async def test_admin_route_forbidden_for_customer_allowed_for_admin(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    async def admin_only(_: User = Depends(require_admin)) -> dict[str, str]:
        return {"ok": "admin"}

    app.add_api_route("/admin-only", admin_only)
    pair = await tokens(client)
    headers = {"Authorization": f"Bearer {pair['access_token']}"}

    assert (await client.get("/admin-only", headers=headers)).status_code == 403

    async with app.state.resources.engine.begin() as conn:
        await conn.execute(text("UPDATE users SET role = 'admin'"))
    assert (await client.get("/admin-only", headers=headers)).status_code == 200


async def test_deactivated_user_loses_access_immediately(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    pair = await tokens(client)
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(text("UPDATE users SET is_active = false"))

    response = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {pair['access_token']}"}
    )

    assert response.status_code == 401


# --- Refresh и logout ---


async def test_refresh_rotates_tokens(client: httpx.AsyncClient) -> None:
    pair = await tokens(client)

    response = await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})

    assert response.status_code == 200
    new_pair = response.json()
    assert new_pair["refresh_token"] != pair["refresh_token"]
    me = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {new_pair['access_token']}"}
    )
    assert me.status_code == 200


async def test_reused_refresh_token_revokes_whole_family(client: httpx.AsyncClient) -> None:
    pair = await tokens(client)
    rotated = (
        await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    ).json()

    # Старый (уже использованный) токен предъявлен повторно: считаем его скомпрометированным.
    replay = await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    after_replay = await client.post(
        "/auth/refresh", json={"refresh_token": rotated["refresh_token"]}
    )

    assert replay.status_code == 401
    assert after_replay.status_code == 401  # новый токен той же цепочки тоже отозван


async def test_parallel_refresh_with_same_token_succeeds_only_once(
    client: httpx.AsyncClient,
) -> None:
    pair = await tokens(client)

    responses = await asyncio.gather(
        *(
            client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
            for _ in range(4)
        )
    )

    assert [r.status_code for r in responses].count(200) == 1


async def test_unknown_refresh_token_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post("/auth/refresh", json={"refresh_token": "nope"})

    assert response.status_code == 401


async def test_expired_refresh_token_is_rejected(app: FastAPI, client: httpx.AsyncClient) -> None:
    pair = await tokens(client)
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(text("UPDATE refresh_tokens SET expires_at = now() - interval '1 day'"))

    response = await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})

    assert response.status_code == 401


async def test_logout_revokes_refresh_token_and_is_idempotent(
    client: httpx.AsyncClient,
) -> None:
    pair = await tokens(client)

    first = await client.post("/auth/logout", json={"refresh_token": pair["refresh_token"]})
    second = await client.post("/auth/logout", json={"refresh_token": pair["refresh_token"]})
    refresh = await client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})

    assert first.status_code == second.status_code == 204
    assert refresh.status_code == 401


# --- Rate limiting ---


async def test_login_is_blocked_after_too_many_failures_for_email(
    client: httpx.AsyncClient,
) -> None:
    await register(client)

    statuses = [(await login(client, password="bad-password")).status_code for _ in range(7)]

    assert statuses[:5] == [401] * 5
    assert statuses[5:] == [429, 429]
    blocked = await login(client)  # даже верный пароль не проходит, пока блок активен
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


async def test_successful_login_resets_failure_counter(client: httpx.AsyncClient) -> None:
    await register(client)
    for _ in range(4):
        await login(client, password="bad-password")

    assert (await login(client)).status_code == 200
    for _ in range(4):
        assert (await login(client, password="bad-password")).status_code == 401


async def test_registration_is_limited_per_ip(client: httpx.AsyncClient) -> None:
    statuses = [(await register(client, f"user{i}@example.com")).status_code for i in range(7)]

    assert statuses == [201] * 5 + [429, 429]


async def test_rate_limit_fails_open_when_redis_is_down(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    import dataclasses

    from redis.asyncio import Redis

    broken = Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True)
    app.state.resources = dataclasses.replace(app.state.resources, redis=broken)

    response = await register(client)
    await broken.aclose()

    assert response.status_code == 201  # Redis не источник истины: сбой не ломает регистрацию
