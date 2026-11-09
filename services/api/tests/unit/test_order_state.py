"""Машина состояний заказа: таблица переходов и свойства (Hypothesis)."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from app.modules.orders.state import (
    EVENT_FOR_STATUS,
    STOCK_RELEASING,
    TERMINAL,
    TRANSITIONS,
    InvalidTransitionError,
    OrderStatus,
    can_transition,
    ensure_transition,
    sources_for,
)

STATUSES = list(OrderStatus)


# --------------------------------------------------------------------------------- таблица переходов
def test_every_status_is_described() -> None:
    assert set(TRANSITIONS) == set(OrderStatus)


def test_only_pending_payment_has_outgoing_transitions() -> None:
    assert TRANSITIONS[OrderStatus.PENDING_PAYMENT] == {
        OrderStatus.PAID,
        OrderStatus.PAYMENT_FAILED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    }
    assert {
        OrderStatus.PAID,
        OrderStatus.PAYMENT_FAILED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    } == TERMINAL


@pytest.mark.parametrize("target", [s for s in OrderStatus if s != OrderStatus.PENDING_PAYMENT])
def test_each_final_status_is_reachable_only_from_pending(target: OrderStatus) -> None:
    assert sources_for(target) == {OrderStatus.PENDING_PAYMENT}


def test_nothing_returns_to_pending_payment() -> None:
    assert sources_for(OrderStatus.PENDING_PAYMENT) == frozenset()


def test_invalid_transition_is_an_error_not_a_silent_overwrite() -> None:
    with pytest.raises(InvalidTransitionError) as error:
        ensure_transition(OrderStatus.EXPIRED, OrderStatus.PAID)

    assert error.value.status_code == 409


def test_stock_is_released_exactly_for_statuses_that_cancel_the_reservation() -> None:
    assert {
        OrderStatus.PAYMENT_FAILED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    } == STOCK_RELEASING
    assert OrderStatus.PAID not in STOCK_RELEASING  # оплаченный товар остаётся проданным


def test_every_reachable_status_has_an_event() -> None:
    assert set(EVENT_FOR_STATUS) == set(OrderStatus) - {OrderStatus.PENDING_PAYMENT}


# --------------------------------------------------------------------------------- свойства
@given(st.lists(st.sampled_from(STATUSES), min_size=1, max_size=30))
def test_random_sequence_of_events_never_breaks_the_state_machine(
    targets: list[OrderStatus],
) -> None:
    """Из любой последовательности попыток смены статуса заказ ведёт себя по таблице."""
    status = OrderStatus.PENDING_PAYMENT
    history = [status]
    for target in targets:
        if can_transition(status, target):
            status = target
        else:
            with pytest.raises(InvalidTransitionError):
                ensure_transition(status, target)
        history.append(status)

    assert status in OrderStatus
    # Заказ меняет статус не более одного раза: все переходы идут только из PENDING_PAYMENT.
    assert len(set(history)) <= 2
    # Достигнув конечного статуса, заказ остаётся в нём навсегда.
    if status in TERMINAL:
        first = history.index(status)
        assert set(history[first:]) == {status}


@given(st.sampled_from(STATUSES), st.sampled_from(STATUSES))
def test_can_transition_agrees_with_sources_for(current: OrderStatus, target: OrderStatus) -> None:
    """Условие SQL (WHERE status IN sources_for(target)) разрешает ровно те же переходы, что и таблица."""
    assert (current in sources_for(target)) == can_transition(current, target)


# --------------------------------------------------------------------------------- модель склада
class InventoryMachine(RuleBasedStateMachine):
    """Случайные заказы и смены статусов над простой моделью склада.

    Проверяется главный инвариант: товар не создаётся и не пропадает.
        остаток + (зарезервировано и оплачено) == начальный запас
    """

    INITIAL = 7

    def __init__(self) -> None:
        super().__init__()
        self.stock = self.INITIAL
        self.orders: list[list[object]] = []  # [количество, статус]

    @initialize()
    def start(self) -> None:
        self.stock = self.INITIAL
        self.orders = []

    @rule(quantity=st.integers(min_value=1, max_value=4))
    def place_order(self, quantity: int) -> None:
        if self.stock >= quantity:  # резерв только при достаточном остатке
            self.stock -= quantity
            self.orders.append([quantity, OrderStatus.PENDING_PAYMENT])

    @rule(index=st.integers(min_value=0, max_value=20), new_status=st.sampled_from(STATUSES))
    def change_status(self, index: int, new_status: OrderStatus) -> None:
        if not self.orders:
            return
        order = self.orders[index % len(self.orders)]
        quantity, current = order
        assert isinstance(quantity, int)
        assert isinstance(current, OrderStatus)
        if can_transition(current, new_status):
            order[1] = new_status
            if new_status in STOCK_RELEASING:
                self.stock += quantity
        else:
            with pytest.raises(InvalidTransitionError):
                ensure_transition(current, new_status)

    @invariant()
    def stock_is_never_negative(self) -> None:
        assert self.stock >= 0

    @invariant()
    def stock_is_conserved(self) -> None:
        held = sum(
            int(q)  # type: ignore[call-overload]
            for q, status in self.orders
            if status in (OrderStatus.PENDING_PAYMENT, OrderStatus.PAID)
        )
        assert self.stock + held == self.INITIAL


TestInventoryMachine = InventoryMachine.TestCase
TestInventoryMachine.settings = settings(max_examples=60, stateful_step_count=25, deadline=None)
