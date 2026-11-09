"""Приём заявок на ремонт."""

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, ConflictError
from app.core.events import EventSink
from app.core.idempotency import fingerprint
from app.core.ratelimit import RateLimiter
from app.modules.repair.models import RepairRequest, RepairStatus
from app.modules.repair.schemas import RepairRequestIn

log = structlog.get_logger("repair")


class RepairService:
    def __init__(
        self, session: AsyncSession, settings: Settings, limiter: RateLimiter, events: EventSink
    ) -> None:
        self._session = session
        self._settings = settings
        self._limiter = limiter
        self._events = events

    async def submit(
        self, data: RepairRequestIn, key: str, client_ip: str
    ) -> tuple[int, dict[str, Any]]:
        """Возвращает (HTTP-статус, тело). 201 - заявка создана, 200 - повтор того же запроса."""
        s = self._settings
        await self._limiter.check(
            f"repair:ip:{client_ip}", s.repair_ip_limit, s.repair_window_seconds
        )

        if data.website:
            # Бот заполнил скрытое поле. Отвечаем так же, как при успехе, чтобы он не понял, что его заметили
            # и не подстроился; в базу и в письма такая заявка не попадает.
            log.warning("repair_honeypot_triggered")
            return 201, {"id": str(uuid.uuid4()), "status": RepairStatus.NEW.value}

        request_hash = fingerprint(
            data.name, data.phone, data.email, data.vehicle, data.injector, data.description
        )
        # Атомарно: либо вставили новую заявку, либо ключ уже занят (повтор или одновременный дубль).
        inserted = await self._session.execute(
            pg_insert(RepairRequest)
            .values(
                id=uuid.uuid4(),
                idempotency_key=key,
                request_hash=request_hash,
                status=RepairStatus.NEW,
                name=data.name,
                phone=data.phone,
                email=data.email,
                vehicle=data.vehicle,
                injector=data.injector,
                description=data.description,
            )
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(RepairRequest.id)
        )
        request_id = inserted.scalar_one_or_none()
        if request_id is not None:
            # Событие и заявка попадают в БД одной транзакцией (outbox). В событии только id: контакты
            # клиента не нужно гонять через брокер, обработчик прочитает их из БД.
            await self._events.publish("repair.requested", {"request_id": str(request_id)})
            await self._session.commit()
            log.info("repair_requested", request_id=str(request_id))
            return 201, {"id": str(request_id), "status": RepairStatus.NEW.value}

        existing = (
            await self._session.execute(
                select(RepairRequest).where(RepairRequest.idempotency_key == key)
            )
        ).scalar_one_or_none()
        if existing is None:
            raise ConflictError("Повторите запрос")
        if existing.request_hash != request_hash:
            raise AppError("Idempotency-Key уже использован для другой заявки", status_code=422)
        return 200, {"id": str(existing.id), "status": existing.status.value}
