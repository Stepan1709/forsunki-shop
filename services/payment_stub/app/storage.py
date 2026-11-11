"""Хранилище заглушки: SQLite. Заглушка имитирует чужую систему со своей базой, поэтому к Postgres магазина не подключается."""

import asyncio
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    order_id TEXT NOT NULL,
    amount_kopecks INTEGER NOT NULL,
    currency TEXT NOT NULL,
    description TEXT NOT NULL,
    return_url TEXT NOT NULL,
    correlation_id TEXT,
    traceparent TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    event_id TEXT,
    created_at TEXT NOT NULL,
    finalized_at TEXT,
    webhook_delivered_at TEXT,
    webhook_attempts INTEGER NOT NULL DEFAULT 0
)
"""


@dataclass(frozen=True)
class Payment:
    id: str
    idempotency_key: str
    order_id: str
    amount_kopecks: int
    currency: str
    description: str
    return_url: str
    correlation_id: str | None
    traceparent: (
        str | None
    )  # контекст трассы запроса «создать платёж»: webhook продолжит ту же трассу
    status: str  # pending | succeeded | failed
    event_id: str | None
    created_at: str
    finalized_at: str | None
    webhook_delivered_at: str | None
    webhook_attempts: int


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    """Синхронный sqlite3 в потоке (to_thread): стандартная библиотека, без лишних зависимостей."""

    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()  # sqlite3 в одном соединении: доступ по очереди
        with self._lock:
            self._conn.execute(_SCHEMA)
            # База могла быть создана до появления колонки (том переживает обновление образа): добавляем при необходимости.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(payments)")}
            if "traceparent" not in columns:
                self._conn.execute("ALTER TABLE payments ADD COLUMN traceparent TEXT")
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _row(self, row: sqlite3.Row | None) -> Payment | None:
        return Payment(**dict(row)) if row else None

    # --- синхронные операции (вызываются через to_thread) ---
    def _create_or_get(self, fields: dict[str, object]) -> tuple[Payment, bool]:
        with self._lock:
            existing = self._conn.execute(
                "SELECT * FROM payments WHERE idempotency_key = ?", (fields["idempotency_key"],)
            ).fetchone()
            if existing:
                return self._row(existing), False  # type: ignore[return-value]
            payment_id = uuid.uuid4().hex
            self._conn.execute(
                "INSERT INTO payments (id, idempotency_key, order_id, amount_kopecks, currency,"
                " description, return_url, correlation_id, traceparent, created_at)"
                " VALUES (:id, :idempotency_key, :order_id, :amount_kopecks, :currency,"
                " :description, :return_url, :correlation_id, :traceparent, :created_at)",
                # traceparent необязателен: вызывающий код может не знать про трассировку.
                {"traceparent": None, **fields, "id": payment_id, "created_at": _now()},
            )
            self._conn.commit()
            created = self._conn.execute(
                "SELECT * FROM payments WHERE id = ?", (payment_id,)
            ).fetchone()
            return self._row(created), True  # type: ignore[return-value]

    def _get(self, payment_id: str) -> Payment | None:
        with self._lock:
            return self._row(
                self._conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            )

    def _finalize(self, payment_id: str, status: str) -> Payment | None:
        """Условный UPDATE: итог фиксируется только один раз (двойной клик не меняет результат)."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE payments SET status = ?, event_id = ?, finalized_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (status, uuid.uuid4().hex, _now(), payment_id),
            )
            self._conn.commit()
            if not cursor.rowcount:
                return None
            return self._row(
                self._conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
            )

    def _mark_delivered(self, payment_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE payments SET webhook_delivered_at = ? WHERE id = ?", (_now(), payment_id)
            )
            self._conn.commit()

    def _bump_attempts(self, payment_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE payments SET webhook_attempts = webhook_attempts + 1 WHERE id = ?",
                (payment_id,),
            )
            self._conn.commit()

    def _undelivered(self, max_attempts: int) -> list[Payment]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM payments WHERE status != 'pending' AND webhook_delivered_at IS NULL"
                " AND webhook_attempts < ?",
                (max_attempts,),
            ).fetchall()
        return [Payment(**dict(r)) for r in rows]

    # --- асинхронные обёртки ---
    async def create_or_get(self, fields: dict[str, object]) -> tuple[Payment, bool]:
        return await asyncio.to_thread(self._create_or_get, fields)

    async def get(self, payment_id: str) -> Payment | None:
        return await asyncio.to_thread(self._get, payment_id)

    async def finalize(self, payment_id: str, status: str) -> Payment | None:
        return await asyncio.to_thread(self._finalize, payment_id, status)

    async def mark_delivered(self, payment_id: str) -> None:
        await asyncio.to_thread(self._mark_delivered, payment_id)

    async def bump_attempts(self, payment_id: str) -> None:
        await asyncio.to_thread(self._bump_attempts, payment_id)

    async def undelivered(self, max_attempts: int) -> list[Payment]:
        return await asyncio.to_thread(self._undelivered, max_attempts)
