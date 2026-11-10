"""Корзина и оформление заказа."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from app.api_client import ApiError
from app.deps import ApiDep, MaybeUserDep, UserDep
from app.errors import is_htmx
from app.templating import render

router = APIRouter()

MAX_QUANTITY = 99


def _cart_context(cart: dict[str, object], error: str | None = None) -> dict[str, object]:
    return {"cart": cart, "error": error, "idem": str(uuid.uuid4())}


def _changed(response: Response) -> Response:
    response.headers["HX-Trigger"] = "cart-changed"  # обновляет значок количества в шапке
    return response


@router.get("/cart")
async def cart_page(request: Request, api: ApiDep, _user: UserDep) -> Response:
    cart = await api.get("/cart", auth=True)
    return render(request, "cart.html", _cart_context(cart))


@router.get("/cart/badge")
async def cart_badge(request: Request, api: ApiDep, user: MaybeUserDep) -> Response:
    count = 0
    if user:
        try:
            cart = await api.get("/cart", auth=True)
            count = sum(line["quantity"] for line in cart["items"])
        except ApiError:
            count = 0  # значок не должен ломать страницу
    return render(request, "partials/_cart_badge.html", {"count": count})


@router.post("/cart/add")
async def cart_add(
    request: Request,
    api: ApiDep,
    _user: UserDep,
    injector_id: Annotated[int, Form()],
    quantity: Annotated[int, Form()] = 1,
) -> Response:
    """Добавляет к уже лежащему в корзине количеству (api хранит абсолютное значение)."""
    current = await api.get("/cart", auth=True)
    existing = next(
        (line["quantity"] for line in current["items"] if line["injector_id"] == injector_id), 0
    )
    wanted = min(MAX_QUANTITY, max(1, existing + max(1, quantity)))
    try:
        await api.put(f"/cart/items/{injector_id}", auth=True, json={"quantity": wanted})
    except ApiError as exc:
        if exc.status not in (409, 422, 404):
            raise
        message = exc.detail if exc.status != 422 else "Недопустимое количество"
        if is_htmx(request):
            return render(request, "partials/_alert.html", {"text": message, "kind": "error"})
        request.state.session.flash(message, "error")
        return RedirectResponse(f"/shop/{injector_id}", status_code=303)
    if is_htmx(request):
        return _changed(render(request, "partials/_added.html", {"quantity": wanted}))
    return RedirectResponse("/cart", status_code=303)


@router.post("/cart/items/{injector_id}")
async def cart_set(
    request: Request,
    api: ApiDep,
    _user: UserDep,
    injector_id: int,
    quantity: Annotated[int, Form()],
) -> Response:
    error: str | None = None
    try:
        await api.put(
            f"/cart/items/{injector_id}",
            auth=True,
            json={"quantity": max(1, min(MAX_QUANTITY, quantity))},
        )
    except ApiError as exc:
        if exc.status not in (409, 422):
            raise
        error = exc.detail
    cart = await api.get("/cart", auth=True)
    name = "partials/_cart.html" if is_htmx(request) else "cart.html"
    response = render(request, name, _cart_context(cart, error))
    return _changed(response) if is_htmx(request) else response


@router.post("/cart/items/{injector_id}/remove")
async def cart_remove(request: Request, api: ApiDep, _user: UserDep, injector_id: int) -> Response:
    await api.delete(f"/cart/items/{injector_id}", auth=True)
    cart = await api.get("/cart", auth=True)
    if is_htmx(request):
        return _changed(render(request, "partials/_cart.html", _cart_context(cart)))
    return RedirectResponse("/cart", status_code=303)


@router.post("/checkout")
async def checkout(
    request: Request, api: ApiDep, _user: UserDep, idem: Annotated[str, Form()] = ""
) -> Response:
    """Оформление заказа. Ключ идемпотентности создан при показе корзины: двойной клик даёт один заказ."""
    try:
        key = str(uuid.UUID(idem))
    except ValueError:
        key = str(uuid.uuid4())
    try:
        order = await api.post("/orders", auth=True, headers={"Idempotency-Key": key})
    except ApiError as exc:
        if exc.status not in (409, 422):
            raise
        cart = await api.get("/cart", auth=True)
        return render(request, "cart.html", _cart_context(cart, exc.detail), 409)
    return RedirectResponse(f"/orders/{order['id']}", status_code=303)
