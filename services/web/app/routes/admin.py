"""Управление товарами (только администратор): список, создание, правка, картинка.

Роль проверяется дважды: здесь (чтобы не показывать обычному пользователю страницу, которая ему не нужна)
и в api на каждый запрос (настоящая защита). Сайт ничего не решает сам, он только передаёт токен пользователя.
"""

from typing import Annotated, Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from app.api_client import ApiError
from app.deps import AdminDep, ApiDep
from app.errors import is_htmx
from app.templating import parse_rubles, render

router = APIRouter(prefix="/admin")

MAX_IMAGE_BYTES = 2 * 1024 * 1024


@router.get("/products")
async def products(
    request: Request, api: ApiDep, _admin: AdminDep, q: str = "", cursor: str = ""
) -> Response:
    params: dict[str, Any] = {"limit": 20}
    notice = None
    if q.strip():
        if len(q.strip()) >= 3:
            params["q"] = q.strip()
        else:
            notice = "Для поиска введите не менее 3 символов."
    if cursor:
        params["cursor"] = cursor
    page = await api.get("/admin/catalog/injectors", auth=True, params=params)
    context = {"items": page["items"], "next_cursor": page["next_cursor"], "q": q, "notice": notice}
    if is_htmx(request):
        return render(request, "admin/_rows.html", context)
    return render(request, "admin/list.html", context)


async def _form_context(api: ApiDep, **extra: Any) -> dict[str, Any]:
    manufacturers = await api.get("/catalog/manufacturers")
    return {"manufacturers": manufacturers, "form_errors": {}, **extra}


@router.get("/products/new")
async def product_new(request: Request, api: ApiDep, _admin: AdminDep) -> Response:
    context = await _form_context(
        api,
        item=None,
        values={
            "stock": "0",
            "price": "",
            "is_active": True,
            "type": "common_rail",
            "condition": "new",
        },
    )
    return render(request, "admin/form.html", context)


@router.get("/products/{injector_id}")
async def product_edit(
    request: Request, api: ApiDep, _admin: AdminDep, injector_id: int
) -> Response:
    item = await api.get(f"/admin/catalog/injectors/{injector_id}", auth=True)
    values = {
        "oem_number": item["oem_number"],
        "title": item["title"],
        "description": item["description"],
        "manufacturer_id": item["manufacturer"]["id"],
        "type": item["type"],
        "condition": item["condition"],
        "price": f"{item['price_kopecks'] / 100:.2f}",
        "stock": item["stock"],
        "is_active": item["is_active"],
        "engines": "\n".join(e["name"] for e in item["engines"]),
    }
    return render(request, "admin/form.html", await _form_context(api, item=item, values=values))


def _payload(form: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Поля формы -> тело запроса к api. Возвращает также ошибки, найденные до отправки."""
    errors: dict[str, str] = {}
    price = parse_rubles(str(form["price"]))
    if price is None:
        errors["price"] = "Введите цену в рублях, например 45000 или 45000,50"
    try:
        stock = int(str(form["stock"]))
        if stock < 0:
            raise ValueError
    except ValueError:
        stock = 0
        errors["stock"] = "Остаток — целое число от 0"
    try:
        manufacturer_id = int(str(form["manufacturer_id"]))
    except ValueError:
        manufacturer_id = 0
        errors["manufacturer_id"] = "Выберите производителя"
    engines = [line.strip() for line in str(form["engines"]).splitlines() if line.strip()]
    payload = {
        "oem_number": str(form["oem_number"]).strip(),
        "title": str(form["title"]).strip(),
        "description": str(form["description"]).strip(),
        "manufacturer_id": manufacturer_id,
        "type": form["type"],
        "condition": form["condition"],
        "price_kopecks": price if price is not None else 0,
        "stock": stock,
        "is_active": bool(form["is_active"]),
        "engines": engines,
    }
    return payload, errors


async def _save(
    request: Request, api: ApiDep, injector_id: int | None, form: dict[str, Any]
) -> Response:
    payload, errors = _payload(form)
    item: dict[str, Any] | None = None
    if injector_id is not None:
        item = await api.get(f"/admin/catalog/injectors/{injector_id}", auth=True)
    if not errors:
        try:
            if injector_id is None:
                saved = await api.post("/admin/catalog/injectors", auth=True, json=payload)
            else:
                saved = await api.patch(
                    f"/admin/catalog/injectors/{injector_id}", auth=True, json=payload
                )
        except ApiError as exc:
            if exc.status not in (404, 409, 422):
                raise
            errors = exc.field_errors or {"_": exc.detail}
            if exc.status == 409:
                errors = {"oem_number": exc.detail}
            if exc.status == 404 and not exc.field_errors:
                errors = {"manufacturer_id": exc.detail}
        else:
            request.state.session.flash("Товар сохранён.", "success")
            return RedirectResponse(f"/admin/products/{saved['id']}", status_code=303)
    values = {**form, "price": form["price"], "engines": form["engines"]}
    context = await _form_context(api, item=item, values=values, form_errors=errors)
    return render(request, "admin/form.html", context, 422)


def _form(
    oem_number: str,
    title: str,
    description: str,
    manufacturer_id: str,
    type_: str,
    condition: str,
    price: str,
    stock: str,
    is_active: str | None,
    engines: str,
) -> dict[str, Any]:
    return {
        "oem_number": oem_number,
        "title": title,
        "description": description,
        "manufacturer_id": manufacturer_id,
        "type": type_,
        "condition": condition,
        "price": price,
        "stock": stock,
        "is_active": is_active is not None,
        "engines": engines,
    }


@router.post("/products")
async def product_create(
    request: Request,
    api: ApiDep,
    _admin: AdminDep,
    oem_number: Annotated[str, Form()],
    title: Annotated[str, Form()],
    manufacturer_id: Annotated[str, Form()],
    type: Annotated[str, Form()],
    condition: Annotated[str, Form()],
    price: Annotated[str, Form()],
    stock: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    engines: Annotated[str, Form()] = "",
) -> Response:
    form = _form(
        oem_number,
        title,
        description,
        manufacturer_id,
        type,
        condition,
        price,
        stock,
        is_active,
        engines,
    )
    return await _save(request, api, None, form)


@router.post("/products/{injector_id}")
async def product_update(
    request: Request,
    api: ApiDep,
    _admin: AdminDep,
    injector_id: int,
    oem_number: Annotated[str, Form()],
    title: Annotated[str, Form()],
    manufacturer_id: Annotated[str, Form()],
    type: Annotated[str, Form()],
    condition: Annotated[str, Form()],
    price: Annotated[str, Form()],
    stock: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    is_active: Annotated[str | None, Form()] = None,
    engines: Annotated[str, Form()] = "",
) -> Response:
    form = _form(
        oem_number,
        title,
        description,
        manufacturer_id,
        type,
        condition,
        price,
        stock,
        is_active,
        engines,
    )
    return await _save(request, api, injector_id, form)


@router.post("/products/{injector_id}/image")
async def product_image(
    request: Request,
    api: ApiDep,
    _admin: AdminDep,
    injector_id: int,
    file: Annotated[UploadFile, File()],
) -> Response:
    # На 1 байт больше лимита: так видно превышение, не читая в память файл любого размера.
    data = await file.read(MAX_IMAGE_BYTES + 1)
    session = request.state.session
    if not data:
        session.flash("Выберите файл с картинкой.", "error")
    elif len(data) > MAX_IMAGE_BYTES:
        session.flash("Файл слишком большой: не больше 2 МБ.", "error")
    else:
        try:
            await api.put(
                f"/catalog/injectors/{injector_id}/image",
                auth=True,
                files={"file": (file.filename or "image", data, file.content_type or "")},
            )
            session.flash("Картинка загружена.", "success")
        except ApiError as exc:
            if exc.status not in (413, 415, 404):
                raise
            session.flash(exc.detail, "error")
    return RedirectResponse(f"/admin/products/{injector_id}", status_code=303)
