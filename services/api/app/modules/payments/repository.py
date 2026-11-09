import uuid
from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.payments.models import Payment, PaymentStatus, ProviderEvent


class PaymentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add(self, payment: Payment) -> None:
        self._session.add(payment)

    async def latest_for_order(self, order_id: uuid.UUID) -> Payment | None:
        result = await self._session.execute(
            select(Payment)
            .where(Payment.order_id == order_id)
            .order_by(Payment.attempt.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def count_for_order(self, order_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(Payment).where(Payment.order_id == order_id)
        )
        return int(result.scalar_one())

    async def get_by_provider_id(self, provider: str, provider_payment_id: str) -> Payment | None:
        result = await self._session.execute(
            select(Payment)
            .where(Payment.provider == provider, Payment.provider_payment_id == provider_payment_id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def transition(
        self,
        payment_id: uuid.UUID,
        sources: Iterable[PaymentStatus],
        target: PaymentStatus,
    ) -> bool:
        """Условная смена статуса платежа (UPDATE ... WHERE status IN (...)): True, если её выполнили мы."""
        result = await self._session.execute(
            update(Payment)
            .where(Payment.id == payment_id, Payment.status.in_(list(sources)))
            .values(status=target, updated_at=func.now())
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def register_event(self, provider: str, event_id: str, event_type: str) -> bool:
        """True, если событие новое. Повторная доставка того же event_id возвращает False (дубль)."""
        result = await self._session.execute(
            pg_insert(ProviderEvent)
            .values(provider=provider, event_id=event_id, event_type=event_type)
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def set_event_outcome(self, provider: str, event_id: str, outcome: str) -> None:
        await self._session.execute(
            update(ProviderEvent)
            .where(ProviderEvent.provider == provider, ProviderEvent.event_id == event_id)
            .values(outcome=outcome)
        )
