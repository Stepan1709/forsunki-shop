"""Страница «О нас»: публичный ответ, редактирование блоков и частых вопросов администратором."""

import httpx

PUBLIC = "/content/about"
ADMIN = "/admin/content"


async def add_faq(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    question: str,
    answer: str = "Ответ",
    **extra: object,
) -> dict[str, object]:
    response = await client.post(
        f"{ADMIN}/faq", json={"question": question, "answer": answer, **extra}, headers=headers
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


async def public_questions(client: httpx.AsyncClient) -> list[str]:
    return [i["question"] for i in (await client.get(PUBLIC)).json()["faq"]]


# --- публичная часть ---


async def test_about_page_has_default_texts_when_nothing_is_saved(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get(PUBLIC)).json()
    assert body["shop"]["slug"] == "about_shop"
    assert body["shop"]["title"]
    assert body["shop"]["body"]
    assert "ТНВД" in body["repair"]["body"]
    assert body["faq"] == []  # вопросы, в отличие от текстов, по умолчанию не подставляются


async def test_public_about_hides_inactive_questions(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    await add_faq(client, admin_headers, "Видимый вопрос")
    await add_faq(client, admin_headers, "Скрытый вопрос", is_active=False)

    assert await public_questions(client) == ["Видимый вопрос"]
    admin_view = (await client.get(ADMIN, headers=admin_headers)).json()
    assert [i["question"] for i in admin_view["faq"]] == ["Видимый вопрос", "Скрытый вопрос"]


# --- права ---


async def test_admin_content_api_requires_admin_role(
    client: httpx.AsyncClient, customer_headers: dict[str, str]
) -> None:
    calls = [
        ("GET", ADMIN, None),
        ("PUT", f"{ADMIN}/pages/about_shop", {"title": "Тема", "body": "Текст"}),
        ("POST", f"{ADMIN}/faq", {"question": "Вопрос?", "answer": "Ответ"}),
        ("PATCH", f"{ADMIN}/faq/1", {"answer": "x"}),
        ("DELETE", f"{ADMIN}/faq/1", None),
        ("POST", f"{ADMIN}/faq/1/move?direction=up", None),
    ]
    for method, url, body in calls:
        assert (await client.request(method, url, json=body)).status_code == 401, (method, url)
        denied = await client.request(method, url, json=body, headers=customer_headers)
        assert denied.status_code == 403, (method, url)


# --- тексты ---


async def test_page_update_is_visible_to_visitors_immediately(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    before = (await client.get(PUBLIC)).json()  # прогревает кэш старым текстом
    assert before["shop"]["title"] != "Наш магазин"

    response = await client.put(
        f"{ADMIN}/pages/about_shop",
        json={"title": "  Наш магазин  ", "body": "Первый абзац.\n\nВторой абзац."},
        headers=admin_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["title"] == "Наш магазин"  # лишние пробелы по краям убраны

    after = (await client.get(PUBLIC)).json()
    assert after["shop"] == {
        "slug": "about_shop",
        "title": "Наш магазин",
        "body": "Первый абзац.\n\nВторой абзац.",
    }
    assert after["repair"] == before["repair"]  # соседний блок не затронут

    # Повторное сохранение обновляет ту же строку, а не создаёт вторую.
    await client.put(
        f"{ADMIN}/pages/about_shop",
        json={"title": "Снова", "body": "Новый текст"},
        headers=admin_headers,
    )
    assert (await client.get(PUBLIC)).json()["shop"]["title"] == "Снова"


async def test_unknown_page_and_invalid_page_bodies_are_rejected(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    unknown = await client.put(
        f"{ADMIN}/pages/other", json={"title": "Тема", "body": "Текст"}, headers=admin_headers
    )
    assert unknown.status_code == 404
    empty = await client.put(
        f"{ADMIN}/pages/about_shop", json={"title": "Тема", "body": ""}, headers=admin_headers
    )
    assert empty.status_code == 422
    huge = await client.put(
        f"{ADMIN}/pages/about_shop",
        json={"title": "Тема", "body": "я" * 10_001},
        headers=admin_headers,
    )
    assert huge.status_code == 422


# --- вопросы ---


async def test_new_questions_are_appended_in_order(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    first = await add_faq(client, admin_headers, "Первый вопрос")
    second = await add_faq(client, admin_headers, "Второй вопрос")
    assert first["position"] == 1
    assert second["position"] == 2
    assert await public_questions(client) == ["Первый вопрос", "Второй вопрос"]


async def test_question_can_be_edited_hidden_and_deleted(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    item = await add_faq(client, admin_headers, "Старый вопрос?", "Старый ответ")
    url = f"{ADMIN}/faq/{item['id']}"
    assert await public_questions(client) == ["Старый вопрос?"]  # кэш прогрет

    edited = await client.patch(
        url, json={"question": "Новый вопрос?", "answer": "Новый ответ"}, headers=admin_headers
    )
    assert edited.json()["question"] == "Новый вопрос?"
    assert (await client.get(PUBLIC)).json()["faq"][0]["answer"] == "Новый ответ"

    # Присланный null не обнуляет обязательное поле.
    kept = await client.patch(url, json={"answer": None}, headers=admin_headers)
    assert kept.json()["answer"] == "Новый ответ"

    await client.patch(url, json={"is_active": False}, headers=admin_headers)
    assert await public_questions(client) == []

    assert (await client.delete(url, headers=admin_headers)).status_code == 204
    assert (await client.delete(url, headers=admin_headers)).status_code == 404
    admin_view = (await client.get(ADMIN, headers=admin_headers)).json()
    assert admin_view["faq"] == []


async def test_invalid_question_payloads_are_rejected(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    for body in (
        {"question": "Да", "answer": "Ответ"},  # слишком короткий вопрос
        {"question": "Нормальный вопрос?", "answer": ""},
        {"question": "Нормальный вопрос?", "answer": "я" * 5_001},
    ):
        response = await client.post(f"{ADMIN}/faq", json=body, headers=admin_headers)
        assert response.status_code == 422, body


async def test_questions_can_be_moved_up_and_down(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    a = await add_faq(client, admin_headers, "Вопрос A")
    b = await add_faq(client, admin_headers, "Вопрос B")
    c = await add_faq(client, admin_headers, "Вопрос C")

    async def move(item: dict[str, object], direction: str) -> httpx.Response:
        return await client.post(
            f"{ADMIN}/faq/{item['id']}/move", params={"direction": direction}, headers=admin_headers
        )

    assert (await move(c, "up")).status_code == 204
    assert await public_questions(client) == ["Вопрос A", "Вопрос C", "Вопрос B"]
    await move(a, "down")
    assert await public_questions(client) == ["Вопрос C", "Вопрос A", "Вопрос B"]

    # На краях списка ничего не меняется и это не ошибка.
    assert (await move(c, "up")).status_code == 204
    assert (await move(b, "down")).status_code == 204
    assert await public_questions(client) == ["Вопрос C", "Вопрос A", "Вопрос B"]


async def test_move_validates_input(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    item = await add_faq(client, admin_headers, "Единственный вопрос")
    sideways = await client.post(
        f"{ADMIN}/faq/{item['id']}/move", params={"direction": "left"}, headers=admin_headers
    )
    assert sideways.status_code == 422
    missing = await client.post(
        f"{ADMIN}/faq/9999/move", params={"direction": "up"}, headers=admin_headers
    )
    assert missing.status_code == 404
    assert (
        await client.patch(f"{ADMIN}/faq/9999", json={"answer": "x"}, headers=admin_headers)
    ).status_code == 404


async def test_deleted_question_leaves_order_of_the_rest_intact(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    a = await add_faq(client, admin_headers, "Вопрос A")
    b = await add_faq(client, admin_headers, "Вопрос B")
    await add_faq(client, admin_headers, "Вопрос C")
    await client.delete(f"{ADMIN}/faq/{b['id']}", headers=admin_headers)
    # Новый вопрос после удаления из середины идёт в конец, а не занимает освободившееся место.
    await add_faq(client, admin_headers, "Вопрос D")
    assert await public_questions(client) == ["Вопрос A", "Вопрос C", "Вопрос D"]
    assert a["position"] == 1
