"""Страница «О нас»: тексты о магазине и сервисе ремонта и частые вопросы (редактирует администратор)."""

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.deps import ApiDep
from app.templating import render

router = APIRouter()


@router.get("/about")
async def about(request: Request, api: ApiDep) -> Response:
    # Если api недоступен, общий обработчик покажет страницу «сервис временно недоступен»: своих данных
    # у этой страницы нет, подставлять нечего.
    content = await api.get("/content/about")
    return render(request, "about.html", {"content": content})
