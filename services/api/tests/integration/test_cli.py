import httpx
from fastapi import FastAPI
from sqlalchemy import text

from app.cli import create_admin
from app.core.config import Settings


async def test_create_admin_makes_user_who_can_log_in(
    infra_settings: Settings, app: FastAPI, client: httpx.AsyncClient
) -> None:
    await create_admin(infra_settings, "  Boss@Example.com ", "admin-pass-123")

    async with app.state.resources.engine.connect() as conn:
        row = (await conn.execute(text("SELECT email, role FROM users"))).one()
    login = await client.post(
        "/auth/login", json={"email": "boss@example.com", "password": "admin-pass-123"}
    )

    assert tuple(row) == ("boss@example.com", "admin")
    assert login.status_code == 200
