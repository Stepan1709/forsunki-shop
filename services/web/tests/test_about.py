"""Страница «О нас» и её редактирование администратором."""

import httpx
from httpx import Response

from tests.conftest import csrf_of
from tests.fake_api import FakeApi

# ------------------------------------------------------------------------------------ публичная страница


async def test_about_page_shows_blocks_and_faq_for_guest(client: httpx.AsyncClient) -> None:
    page = await client.get("/about")
    assert page.status_code == 200
    assert "О магазине" in page.text
    assert "Сервис ремонта" in page.text
    assert "Ремонтируем ТНВД." in page.text
    assert "Частые вопросы" in page.text
    assert "Как оплатить заказ?" in page.text
    assert page.text.count('<details class="faq card">') == 2  # раскрывающиеся вопросы на <details>


async def test_about_text_is_split_into_paragraphs(client: httpx.AsyncClient) -> None:
    page = await client.get("/about")
    # «Первый абзац.\n\nВторой абзац.» превращается в два отдельных <p>, а не в один с переносом.
    assert ">Первый абзац.</p>" in page.text
    assert ">Второй абзац.</p>" in page.text
    assert ">Да.</p>" in page.text
    assert ">Звоните.</p>" in page.text


async def test_about_text_from_database_is_escaped(client: httpx.AsyncClient, api: FakeApi) -> None:
    from tests.fake_api import ABOUT

    evil = {
        **ABOUT,
        "shop": {
            "slug": "about_shop",
            "title": "<script>alert(1)</script>",
            "body": "<b>жирный</b>",
        },
        "faq": [{"id": 1, "question": "<img src=x onerror=alert(1)>", "answer": "<i>ответ</i>"}],
    }
    api.overrides["/content/about"] = lambda call: Response(200, json=evil)
    page = await client.get("/about")
    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<b>жирный</b>" not in page.text
    assert "<img src=x" not in page.text
    assert "<i>ответ</i>" not in page.text


async def test_about_page_without_questions_has_no_faq_section(
    client: httpx.AsyncClient, api: FakeApi
) -> None:
    from tests.fake_api import ABOUT

    api.overrides["/content/about"] = lambda call: Response(200, json={**ABOUT, "faq": []})
    page = await client.get("/about")
    assert page.status_code == 200
    assert "Частые вопросы" not in page.text


async def test_about_page_when_api_is_down_shows_service_page(
    client: httpx.AsyncClient, api: FakeApi
) -> None:
    api.overrides["/content/about"] = lambda call: Response(503, json={"detail": "down"})
    page = await client.get("/about")
    assert page.status_code in (502, 503)


async def test_menu_has_about_link_and_admin_links_only_for_admin(
    client: httpx.AsyncClient,
) -> None:
    guest = await client.get("/")
    assert 'href="/about"' in guest.text
    assert 'href="/admin/about"' not in guest.text


async def test_admin_sees_texts_link_in_menu(admin: httpx.AsyncClient) -> None:
    page = await admin.get("/about")
    assert 'href="/admin/about"' in page.text


# ------------------------------------------------------------------------------------ редактирование


async def test_admin_about_page_lists_texts_and_questions(admin: httpx.AsyncClient) -> None:
    page = await admin.get("/admin/about")
    assert page.status_code == 200
    assert 'action="/admin/about/pages/about_shop"' in page.text
    assert 'action="/admin/about/pages/about_repair"' in page.text
    assert "Ремонтируем ТНВД." in page.text
    assert 'action="/admin/about/faq/1/move"' in page.text
    assert "Добавить вопрос" in page.text


async def test_admin_about_is_closed_to_guests_and_customers(
    buyer: httpx.AsyncClient,
) -> None:
    response = await buyer.get("/admin/about")
    assert response.status_code == 403


async def test_admin_saves_page_text(admin: httpx.AsyncClient, api: FakeApi) -> None:
    token = csrf_of((await admin.get("/admin/about")).text)
    response = await admin.post(
        "/admin/about/pages/about_shop",
        data={"csrf_token": token, "title": "Наш магазин", "body": "Строка 1\r\n\r\nСтрока 2"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/about"
    sent = api.called("PUT", "/admin/content/pages/about_shop")[-1].json
    assert sent == {"title": "Наш магазин", "body": "Строка 1\n\nСтрока 2"}  # CRLF приведён к LF


async def test_admin_page_save_rejects_empty_and_unknown_without_calling_api(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    token = csrf_of((await admin.get("/admin/about")).text)
    empty = await admin.post(
        "/admin/about/pages/about_shop", data={"csrf_token": token, "title": "Т", "body": " "}
    )
    unknown = await admin.post(
        "/admin/about/pages/secret", data={"csrf_token": token, "title": "Тема", "body": "Текст"}
    )
    assert empty.status_code == 303
    assert unknown.status_code == 303
    assert not api.called("PUT", "/admin/content/pages/about_shop")
    assert not api.called("PUT", "/admin/content/pages/secret")
    message = await admin.get("/admin/about")
    assert "Заполните заголовок и текст блока." in message.text


async def test_admin_creates_updates_deletes_and_moves_questions(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    token = csrf_of((await admin.get("/admin/about")).text)

    created = await admin.post(
        "/admin/about/faq",
        data={
            "csrf_token": token,
            "question": "Новый вопрос?",
            "answer": "Ответ",
            "is_active": "1",
        },
    )
    assert created.status_code == 303
    assert api.called("POST", "/admin/content/faq")[-1].json == {
        "question": "Новый вопрос?",
        "answer": "Ответ",
        "is_active": True,
    }

    # Галочка «Показывать» снята: поле не приходит, вопрос скрывается.
    await admin.post(
        "/admin/about/faq/1", data={"csrf_token": token, "question": "Вопрос?", "answer": "Ответ"}
    )
    assert api.called("PATCH", "/admin/content/faq/1")[-1].json["is_active"] is False

    await admin.post("/admin/about/faq/1/move", data={"csrf_token": token, "direction": "down"})
    assert api.called("POST", "/admin/content/faq/1/move")[-1].params == {"direction": "down"}

    await admin.post("/admin/about/faq/2/delete", data={"csrf_token": token})
    assert api.called("DELETE", "/admin/content/faq/2")


async def test_admin_question_forms_validate_before_calling_api(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    token = csrf_of((await admin.get("/admin/about")).text)
    await admin.post(
        "/admin/about/faq", data={"csrf_token": token, "question": "Да", "answer": "x"}
    )
    await admin.post("/admin/about/faq/1/move", data={"csrf_token": token, "direction": "sideways"})
    assert not api.called("POST", "/admin/content/faq")
    assert not api.called("POST", "/admin/content/faq/1/move")


async def test_admin_about_actions_require_csrf(admin: httpx.AsyncClient, api: FakeApi) -> None:
    assert (await admin.post("/admin/about/faq/1/delete", data={})).status_code == 403
    assert not api.called("DELETE", "/admin/content/faq/1")


async def test_missing_question_is_reported_as_message(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    token = csrf_of((await admin.get("/admin/about")).text)
    response = await admin.post("/admin/about/faq/9999/delete", data={"csrf_token": token})
    assert response.status_code == 303
    assert "Вопрос не найден" in (await admin.get("/admin/about")).text
