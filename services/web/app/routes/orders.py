"""Заказы: профиль с историей, карточка заказа, статус оплаты."""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.api_client import ApiError
from app.deps import ApiDep, UserDep
from app.templating import render

router = APIRouter()


@router.get("/profile")
async def profile(request: Request, api: ApiDep, user: UserDep, cursor: str = "") -> Response:
    params: dict[str, object] = {"limit": 10}
    if cursor:
        params["cursor"] = cursor
    try:
        page = await api.get("/orders", auth=True, params=params)
    except ApiError as exc:
        if exc.status != 422:
            raise
        page = {"items": [], "next_cursor": None}
    return render(
        request, "profile.html", {"orders": page["items"], "next_cursor": page["next_cursor"]}
    )


async def _status_context(api: ApiDep, order_id: uuid.UUID) -> dict[str, object]:
    order = await api.get(f"/orders/{order_id}", auth=True)
    payment: dict[str, object] | None = None
    if order["status"] == "PENDING_PAYMENT":
        try:
            payment = await api.get(f"/orders/{order_id}/payment", auth=True)
        except ApiError as exc:
            if exc.status != 404:
                raise
    return {"order": order, "payment": payment}


@router.get("/orders/{order_id}")
async def order_page(
    request: Request, api: ApiDep, _user: UserDep, order_id: uuid.UUID
) -> Response:
    return render(request, "orders/detail.html", await _status_context(api, order_id))


@router.get("/orders/{order_id}/status")
async def order_status(
    request: Request, api: ApiDep, _user: UserDep, order_id: uuid.UUID
) -> Response:
    """Панель статуса. Пока заказ ждёт оплаты, страница опрашивает этот адрес; после оплаты панель приходит
    без атрибутов опроса, и обновления прекращаются."""
    return render(request, "orders/_status.html", await _status_context(api, order_id))


@router.post("/orders/{order_id}/cancel")
async def order_cancel(
    request: Request, api: ApiDep, _user: UserDep, order_id: uuid.UUID
) -> Response:
    try:
        await api.post(
            f"/orders/{order_id}/cancel", auth=True, headers={"Idempotency-Key": str(uuid.uuid4())}
        )
        request.state.session.flash("Заказ отменён, товар возвращён на склад.", "success")
    except ApiError as exc:
        if exc.status != 409:
            raise
        request.state.session.flash("Заказ уже нельзя отменить.", "error")
    return RedirectResponse(f"/orders/{order_id}", status_code=303)
