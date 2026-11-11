"""Чистка старых служебных строк: outbox, inbox (processed_messages) и idempotency_keys.

Эти таблицы растут с каждым заказом и сообщением, а нужны только ограниченное время:
  * outbox: отправленные события нужны разве что для разбора инцидента; неотправленные (sent_at IS NULL) не трогаем никогда;
  * processed_messages (inbox): защищают от повторной обработки, пока сообщение ещё может прийти повторно. Срок хранения
    должен быть заметно больше самого долгого пути сообщения (повторы с backoff, разбор DLQ вручную), поэтому по умолчанию 14 дней;
  * idempotency_keys: клиент повторяет запрос с тем же ключом в течение минут или часов, а не недель.

Удаление идёт небольшими пачками, каждая в своей транзакции: один огромный DELETE держал бы блокировки, раздувал WAL и
мешал бы автовакууму. Выбор строк идёт по индексам по времени (миграция 0010), а FOR UPDATE SKIP LOCKED позволяет нескольким
воркерам чистить параллельно, не мешая друг другу. Повторный запуск безопасен: уже удалённого просто нет.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.metrics import RETENTION_DELETED

log = structlog.get_logger("retention")


@dataclass(frozen=True)
class _Target:
    table: str
    condition: str  # «строка устарела»; :cutoff это now() минус срок хранения
    key: str  # столбцы, по которым строка находится в подзапросе
    keep_days_attr: str  # имя настройки со сроком хранения


# Имена таблиц и условия заданы в коде, а не приходят снаружи, поэтому подстановка в SQL безопасна.
_TARGETS = (
    _Target("outbox", "sent_at IS NOT NULL AND sent_at < :cutoff", "id", "retention_outbox_days"),
    _Target(
        "processed_messages",
        "processed_at < :cutoff",
        "message_id, consumer",
        "retention_inbox_days",
    ),
    _Target(
        "idempotency_keys", "created_at < :cutoff", "user_id, key", "retention_idempotency_days"
    ),
)


class RetentionCleaner:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    async def _delete_batch(self, target: _Target, days: float) -> int:
        sql = text(
            f"""
            DELETE FROM {target.table}
            WHERE ({target.key}) IN (
                SELECT {target.key} FROM {target.table}
                WHERE {target.condition}
                LIMIT :batch
                FOR UPDATE SKIP LOCKED
            )
            """  # noqa: S608
        )
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self._session_factory() as session:
            result = await session.execute(
                sql, {"cutoff": cutoff, "batch": self._settings.retention_batch_size}
            )
            await session.commit()
        return int(result.rowcount)  # type: ignore[attr-defined]

    async def run_once(self) -> dict[str, int]:
        """Удаляет устаревшее по всем таблицам. Возвращает, сколько строк удалено в каждой."""
        deleted: dict[str, int] = {}
        for target in _TARGETS:
            days = float(getattr(self._settings, target.keep_days_attr))
            total = 0
            while True:
                count = await self._delete_batch(target, days)
                total += count
                if count < self._settings.retention_batch_size:
                    break
                await asyncio.sleep(0)  # отдаём управление: чистка не должна занимать цикл событий
            deleted[target.table] = total
            if total:
                RETENTION_DELETED.labels(table=target.table).inc(total)
                log.info("retention_deleted", table=target.table, count=total)
        return deleted

    async def run(self, stop: asyncio.Event) -> None:
        interval = self._settings.retention_interval_seconds
        while not stop.is_set():
            try:
                await self.run_once()
            except Exception:
                log.exception("retention_iteration_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), interval)
