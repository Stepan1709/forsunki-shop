"""Магазин: список с фильтрами и поиском, карточка товара."""

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import Response

from app.api_client import ApiError
from app.deps import ApiDep
from app.errors import is_htmx
from app.templating import render

router = APIRouter(prefix="/shop")

PAGE_SIZE = 12
MIN_QUERY = 3  # так же, как в api: триграммный поиск работает с 3 символов


@router.get("")
async def shop_list(
    request: Request,
    api: ApiDep,
    manufacturer: str = "",
    type: str = "",
    condition: str = "",
    q: str = "",
    cursor: str = "",
) -> Response:
    q = q.strip()
    notice: str | None = None
    params: dict[str, Any] = {"limit": PAGE_SIZE}
    filters = {"manufacturer": manufacturer, "type": type, "condition": condition}
    params.update({k: v for k, v in filters.items() if v})
    if q:
        if len(q) >= MIN_QUERY:
            params["q"] = q
        else:
            notice = f"Для поиска введите не менее {MIN_QUERY} символов."
    if cursor:
        params["cursor"] = cursor

    try:
        page = await api.get("/catalog/injectors", params=params)
    except ApiError as exc:
        if exc.status != 422:
            raise
        # Повреждённый курсор или значение фильтра из ручной правки адреса: показываем пустой результат.
        page = {"items": [], "next_cursor": None}
        notice = "Проверьте параметры поиска."

    # Строка запроса для «Показать ещё»: те же фильтры плюс курсор следующей страницы.
    keep = {k: v for k, v in {**filters, "q": q if len(q) >= MIN_QUERY else ""}.items() if v}
    next_query = urlencode({**keep, "cursor": page["next_cursor"]}) if page["next_cursor"] else None
    context = {"items": page["items"], "next_query": next_query, "notice": notice}

    if is_htmx(request):
        # «Показать ещё» присылает только следующие карточки, смена фильтра заменяет весь блок результатов.
        name = "shop/_page.html" if cursor else "shop/_results.html"
        return render(request, name, context)

    manufacturers = await api.get("/catalog/manufacturers")
    context.update(
        manufacturers=manufacturers,
        values={**filters, "q": q},
    )
    return render(request, "shop/list.html", context)


@router.get("/{injector_id}")
async def shop_card(request: Request, api: ApiDep, injector_id: int) -> Response:
    card = await api.get(f"/catalog/injectors/{injector_id}")
    return render(request, "shop/card.html", {"item": card})
