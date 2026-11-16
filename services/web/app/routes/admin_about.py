"""Редактирование страницы «О нас» (только администратор): тексты блоков и частые вопросы.

Все изменения идут через админский API; здесь проверяются только очевидные ошибки формы, чтобы не
отправлять заведомо неверный запрос. Роль проверяется и тут (не показывать лишнюю страницу), и в api.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.api_client import ApiError
from app.deps import AdminDep, ApiDep
from app.templating import render

router = APIRouter(prefix="/admin/about")

PAGE_SLUGS = ("about_shop", "about_repair")
BACK = "/admin/about"
_API = "/admin/content"


def _back() -> Response:
    return RedirectResponse(BACK, status_code=303)


async def _call(
    request: Request, api: ApiDep, method: str, path: str, ok: str, **kwargs: Any
) -> None:
    """Вызов админского API. Ожидаемые отказы (нет такого вопроса, неверные данные) показываем сообщением."""
    session = request.state.session
    try:
        await api.request(method, path, auth=True, **kwargs)
        session.flash(ok, "success")
    except ApiError as exc:
        if exc.status not in (404, 422):
            raise
        session.flash(exc.detail, "error")


@router.get("")
async def overview(request: Request, api: ApiDep, _admin: AdminDep) -> Response:
    data = await api.get(_API, auth=True)
    return render(request, "admin/about.html", {"pages": data["pages"], "faq": data["faq"]})


@router.post("/pages/{slug}")
async def save_page(
    request: Request,
    api: ApiDep,
    _admin: AdminDep,
    slug: str,
    title: Annotated[str, Form()],
    body: Annotated[str, Form()],
) -> Response:
    if slug not in PAGE_SLUGS:
        request.state.session.flash("Такого блока нет.", "error")
        return _back()
    # Переводы строк из textarea приходят как CRLF: приводим к LF, чтобы абзацы делились одинаково.
    body = body.replace("\r\n", "\n").strip()
    if len(title.strip()) < 2 or not body:
        request.state.session.flash("Заполните заголовок и текст блока.", "error")
        return _back()
    await _call(
        request,
        api,
        "PUT",
        f"{_API}/pages/{slug}",
        "Текст сохранён.",
        json={"title": title.strip(), "body": body},
    )
    return _back()


@router.post("/faq")
async def create_faq(
    request: Request,
    api: ApiDep,
    _admin: AdminDep,
    question: Annotated[str, Form()],
    answer: Annotated[str, Form()],
    is_active: Annotated[str | None, Form()] = None,
) -> Response:
    answer = answer.replace("\r\n", "\n").strip()
    if len(question.strip()) < 3 or not answer:
        request.state.session.flash("Заполните вопрос и ответ.", "error")
        return _back()
    await _call(
        request,
        api,
        "POST",
        f"{_API}/faq",
        "Вопрос добавлен.",
        json={"question": question.strip(), "answer": answer, "is_active": is_active is not None},
    )
    return _back()


@router.post("/faq/{faq_id}")
async def update_faq(
    request: Request,
    api: ApiDep,
    _admin: AdminDep,
    faq_id: int,
    question: Annotated[str, Form()],
    answer: Annotated[str, Form()],
    is_active: Annotated[str | None, Form()] = None,
) -> Response:
    answer = answer.replace("\r\n", "\n").strip()
    if len(question.strip()) < 3 or not answer:
        request.state.session.flash("Заполните вопрос и ответ.", "error")
        return _back()
    await _call(
        request,
        api,
        "PATCH",
        f"{_API}/faq/{faq_id}",
        "Вопрос сохранён.",
        json={"question": question.strip(), "answer": answer, "is_active": is_active is not None},
    )
    return _back()


@router.post("/faq/{faq_id}/delete")
async def delete_faq(request: Request, api: ApiDep, _admin: AdminDep, faq_id: int) -> Response:
    await _call(request, api, "DELETE", f"{_API}/faq/{faq_id}", "Вопрос удалён.")
    return _back()


@router.post("/faq/{faq_id}/move")
async def move_faq(
    request: Request,
    api: ApiDep,
    _admin: AdminDep,
    faq_id: int,
    direction: Annotated[str, Form()],
) -> Response:
    if direction not in ("up", "down"):
        request.state.session.flash("Неизвестное направление.", "error")
        return _back()
    # Сообщение об успехе не нужно: порядок виден сразу.
    session = request.state.session
    try:
        await api.request(
            "POST", f"{_API}/faq/{faq_id}/move", auth=True, params={"direction": direction}
        )
    except ApiError as exc:
        if exc.status not in (404, 422):
            raise
        session.flash(exc.detail, "error")
    return _back()
