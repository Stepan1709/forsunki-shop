"""Чистка старых служебных строк (outbox, inbox, idempotency_keys). Реальный Postgres."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import text

from app.core.config import Settings
from app.worker.retention import RetentionCleaner
from tests.integration.conftest import create_user


async def count(app: FastAPI, table: str) -> int:
    async with app.state.resources.engine.connect() as conn:
        return int((await conn.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())  # noqa: S608


async def seed_rows(app: FastAPI, user_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """По две строки в каждой таблице: старая (30 дней) и свежая."""
    ids = {"old_sent": uuid.uuid4(), "new_sent": uuid.uuid4(), "old_unsent": uuid.uuid4()}
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    insert_outbox = text(
        "INSERT INTO outbox (id, routing_key, payload, created_at, sent_at) "
        "VALUES (:id, 'x.y', '{}', :created, :sent)"
    )
    insert_inbox = text(
        "INSERT INTO processed_messages (message_id, consumer, processed_at) VALUES (:id, :c, :at)"
    )
    insert_key = text(
        "INSERT INTO idempotency_keys (user_id, key, request_hash, created_at) "
        "VALUES (:u, :k, 'h', :at)"
    )
    async with app.state.resources.engine.begin() as conn:
        # Неотправленное событие (sent=None) не удаляется никогда, как бы давно оно ни лежало.
        for name, sent in (("old_sent", old), ("new_sent", now), ("old_unsent", None)):
            await conn.execute(insert_outbox, {"id": ids[name], "created": old, "sent": sent})
        for at, consumer in ((old, "old"), (now, "new")):
            await conn.execute(insert_inbox, {"id": uuid.uuid4(), "c": consumer, "at": at})
        for at, key in ((old, "old-key-0001"), (now, "new-key-0001")):
            await conn.execute(insert_key, {"u": user_id, "k": key, "at": at})
    return ids


async def test_retention_deletes_only_old_rows_and_keeps_unsent_events(
    app: FastAPI, infra_settings: Settings
) -> None:
    user_id, _ = await create_user(app, "retention@example.com")
    ids = await seed_rows(app, user_id)
    cleaner = RetentionCleaner(app.state.resources.session_factory, infra_settings)

    deleted = await cleaner.run_once()

    assert deleted["outbox"] >= 1
    assert deleted["processed_messages"] >= 1
    assert deleted["idempotency_keys"] >= 1
    async with app.state.resources.engine.connect() as conn:
        left = {
            row[0]
            for row in await conn.execute(
                text("SELECT id FROM outbox WHERE id = ANY(:ids)"), {"ids": list(ids.values())}
            )
        }
        inbox = {r[0] for r in await conn.execute(text("SELECT consumer FROM processed_messages"))}
        keys = {r[0] for r in await conn.execute(text("SELECT key FROM idempotency_keys"))}
    assert left == {ids["new_sent"], ids["old_unsent"]}
    assert "old" not in inbox
    assert "new" in inbox
    assert "old-key-0001" not in keys
    assert "new-key-0001" in keys


async def test_retention_is_idempotent_and_works_in_small_batches(
    app: FastAPI, infra_settings: Settings
) -> None:
    async with app.state.resources.engine.begin() as conn:
        for _ in range(25):
            await conn.execute(
                text(
                    "INSERT INTO processed_messages (message_id, consumer, processed_at) "
                    "VALUES (:id, 'batch', now() - make_interval(days => 60))"
                ),
                {"id": uuid.uuid4()},
            )
    settings = infra_settings.model_copy(update={"retention_batch_size": 10})
    cleaner = RetentionCleaner(app.state.resources.session_factory, settings)

    first = await cleaner.run_once()
    second = await cleaner.run_once()

    assert first["processed_messages"] >= 25  # три пачки по 10, 10 и 5 (плюс чужие старые строки)
    assert second["processed_messages"] == 0
    async with app.state.resources.engine.connect() as conn:
        left = (
            await conn.execute(
                text("SELECT count(*) FROM processed_messages WHERE consumer = 'batch'")
            )
        ).scalar_one()
    assert left == 0
