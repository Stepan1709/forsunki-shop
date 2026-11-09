import pytest

from app.core.errors import AppError
from app.modules.catalog.repository import normalize_oem
from app.modules.catalog.service import decode_cursor, encode_cursor


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0 445 110 183", "0445110183"),
        ("0445-110-183", "0445110183"),
        ("095000-5550", "0950005550"),
        ("a2c 5951_3554", "A2C59513554"),
        ("  ", ""),
    ],
)
def test_normalize_oem(raw: str, expected: str) -> None:
    assert normalize_oem(raw) == expected


def test_cursor_roundtrip() -> None:
    assert decode_cursor(encode_cursor(12345)) == 12345


@pytest.mark.parametrize("bad", ["not-base64!!", "YWJj", "LTU="])  # мусор, "abc", "-5"
def test_invalid_cursor_is_rejected(bad: str) -> None:
    with pytest.raises(AppError) as error:
        decode_cursor(bad)

    assert error.value.status_code == 422
