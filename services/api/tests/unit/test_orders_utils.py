import uuid
from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.core.errors import AppError
from app.core.idempotency import fingerprint, get_idempotency_key
from app.modules.orders.service import decode_cursor, encode_cursor


@given(
    st.datetimes(
        timezones=st.just(UTC), min_value=datetime(2000, 1, 1), max_value=datetime(2100, 1, 1)
    ),
    st.uuids(),
)
def test_cursor_roundtrip_for_any_timestamp_and_id(
    created_at: datetime, order_id: uuid.UUID
) -> None:
    assert decode_cursor(encode_cursor(created_at, order_id)) == (created_at, order_id)


@pytest.mark.parametrize("bad", ["###", "YWJj", "MjAyNi0wMS0wMXxub3QtYS11dWlk"])
def test_invalid_cursor_is_rejected(bad: str) -> None:
    with pytest.raises(AppError) as error:
        decode_cursor(bad)

    assert error.value.status_code == 422


@pytest.mark.parametrize(
    "key", ["8e4c1b7a-4d1e-4f6a-9d2a-0a1b2c3d4e5f", "order_2026-11-09:abc", "A" * 8]
)
def test_valid_idempotency_keys(key: str) -> None:
    assert get_idempotency_key(key) == key


@pytest.mark.parametrize(
    "key", [None, "", "short", "has space in it!", "a" * 129, "кириллица-12345"]
)
def test_invalid_idempotency_keys_are_rejected(key: str | None) -> None:
    with pytest.raises(AppError) as error:
        get_idempotency_key(key)

    assert error.value.status_code == 400


def test_fingerprint_depends_on_every_part() -> None:
    assert fingerprint("POST", "/orders", "u1") == fingerprint("POST", "/orders", "u1")
    assert fingerprint("POST", "/orders", "u1") != fingerprint("POST", "/orders", "u2")
    assert fingerprint("POST", "/orders") != fingerprint("POST", "/orders/1/cancel")
