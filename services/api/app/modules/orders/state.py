"""Машина состояний заказа. Чистая логика без БД: её можно проверять быстро и свойствами (Hypothesis).

    PENDING_PAYMENT ──► PAID
          │         ──► PAYMENT_FAILED
          │         ──► CANCELLED
          └─────────────► EXPIRED
Остальные статусы конечные: из них переходов нет. Недопустимый переход это ошибка, а не молчаливая перезапись:
иначе, например, истёкший заказ мог бы «ожить» поздним событием и забрать товар, который уже вернулся на склад.
"""

import enum

from app.core.errors import AppError


class OrderStatus(enum.StrEnum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


# Единственное место, где записано, какие переходы допустимы.
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_PAYMENT: frozenset(
        {
            OrderStatus.PAID,
            OrderStatus.PAYMENT_FAILED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PAID: frozenset(),
    OrderStatus.PAYMENT_FAILED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}

TERMINAL = frozenset(status for status, targets in TRANSITIONS.items() if not targets)

# При переходе в эти статусы зарезервированный товар возвращается на склад. При PAID резерв превращается в продажу.
STOCK_RELEASING = frozenset(
    {OrderStatus.PAYMENT_FAILED, OrderStatus.CANCELLED, OrderStatus.EXPIRED}
)

# Событие, которое публикуется при переходе в статус (подписчики: платежи, письма).
EVENT_FOR_STATUS: dict[OrderStatus, str] = {
    OrderStatus.PAID: "order.paid",
    OrderStatus.PAYMENT_FAILED: "order.payment_failed",
    OrderStatus.CANCELLED: "order.cancelled",
    OrderStatus.EXPIRED: "order.expired",
}


class InvalidTransitionError(AppError):
    status_code = 409

    def __init__(self, current: OrderStatus, target: OrderStatus) -> None:
        super().__init__(f"Недопустимый переход статуса заказа: {current} -> {target}")
        self.current = current
        self.target = target


def can_transition(current: OrderStatus, target: OrderStatus) -> bool:
    return target in TRANSITIONS[current]


def ensure_transition(current: OrderStatus, target: OrderStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


def sources_for(target: OrderStatus) -> frozenset[OrderStatus]:
    """Из каких статусов можно перейти в `target`. Идёт в условие UPDATE ... WHERE status IN (...)."""
    return frozenset(status for status, targets in TRANSITIONS.items() if target in targets)
