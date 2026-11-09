import uuid

from app.modules.notifications.service import build_message, format_money, short_number


def test_format_money_groups_thousands_and_pads_kopecks() -> None:
    assert format_money(4_500_000) == "45 000,00 ₽"
    assert format_money(5) == "0,05 ₽"
    assert format_money(123_456_789) == "1 234 567,89 ₽"


def test_short_number_is_stable_and_uppercase() -> None:
    value = uuid.UUID("0a1b2c3d-1111-2222-3333-444455556666")
    assert short_number(value) == "0A1B2C3D"
    assert short_number(str(value)) == "0A1B2C3D"


def test_message_has_text_and_html_parts() -> None:
    message = build_message(
        sender="Forsunki <noreply@forsunki.local>",
        to="a@example.com",
        subject="Тема",
        template="welcome",
        context={"shop_url": "http://shop"},
    )
    assert message.is_multipart()
    assert message.get_body(("plain",)) is not None
    assert message.get_body(("html",)) is not None
    assert "http://shop/catalog" in message.get_body(("plain",)).get_content()  # type: ignore[union-attr]
