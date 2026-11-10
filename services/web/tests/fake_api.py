"""Подмена бэкенда `api` на уровне HTTP: сайт делает настоящие запросы httpx, ответы собирает эта заглушка.

Она хранит состояние (корзина, заказы), считает вызовы и запоминает заголовки, чтобы тесты могли проверить, что
сайт передал токен, Idempotency-Key и X-Request-ID.
"""

import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

USERS = {
    "buyer@example.com": ("pass-12345", "customer"),
    "admin@example.com": ("pass-12345", "admin"),
}

INJECTORS: dict[int, dict[str, Any]] = {
    1: {
        "id": 1,
        "oem_number": "0 445 110 183",
        "title": "Форсунка Bosch 0445110183",
        "manufacturer": {"id": 1, "name": "Bosch", "slug": "bosch"},
        "type": "common_rail",
        "condition": "new",
        "price_kopecks": 4_500_000,
        "stock": 5,
        "image_url": None,
        "description": "Для Mercedes OM651",
        "engines": [{"id": 1, "name": "Mercedes-Benz OM651 2.1 CDI"}],
        "is_active": True,
    },
    2: {
        "id": 2,
        "oem_number": "095000-5550",
        "title": "Форсунка Denso <b>095000</b>",
        "manufacturer": {"id": 2, "name": "Denso", "slug": "denso"},
        "type": "common_rail",
        "condition": "remanufactured",
        "price_kopecks": 2_700_000,
        "stock": 0,
        "image_url": "http://s3.test/img.jpg",
        "description": "",
        "engines": [],
        "is_active": False,
    },
}


@dataclass
class Call:
    method: str
    path: str
    headers: dict[str, str]
    json: Any
    params: dict[str, str]


@dataclass
class FakeApi:
    calls: list[Call] = field(default_factory=list)
    carts: dict[str, dict[int, int]] = field(default_factory=dict)
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    order_status: str = "PENDING_PAYMENT"
    payment: dict[str, Any] = field(
        default_factory=lambda: {"status": "PENDING", "pay_url": "http://pay.test/p1"}
    )
    # access-токены, которые api «уже не принимает» (истекли)
    expired_access: set[str] = field(default_factory=set)
    refresh_valid: bool = True
    refresh_count: int = 0
    down: bool = False
    overrides: dict[str, Callable[[Call], httpx.Response]] = field(default_factory=dict)
    _tokens: dict[str, str] = field(default_factory=dict)  # access -> email
    _counter: int = 0

    # ------------------------------------------------------------------------------- вспомогательное
    def called(self, method: str, path: str) -> list[Call]:
        return [c for c in self.calls if c.method == method and c.path == path]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _problem(self, status: int, detail: str, **extra: Any) -> httpx.Response:
        return httpx.Response(
            status,
            json={"title": "x", "status": status, "detail": detail, **extra},
            headers={"content-type": "application/problem+json"},
        )

    def _issue(self, email: str) -> dict[str, Any]:
        self._counter += 1
        access = f"access-{self._counter}-{email}"
        self._tokens[access] = email
        return {
            "access_token": access,
            "refresh_token": f"refresh-{self._counter}-{email}",
            "token_type": "bearer",
            "expires_in": 900,
        }

    def _user(self, call: Call) -> str | None:
        auth = call.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        if token in self.expired_access:
            return None
        return self._tokens.get(token)

    # ------------------------------------------------------------------------------- обработчик
    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("api down")
        body: Any = None
        if request.content and "json" in request.headers.get("content-type", ""):
            body = json.loads(request.content)
        call = Call(
            request.method,
            request.url.path,
            {k.lower(): v for k, v in request.headers.items()},
            body,
            dict(request.url.params),
        )
        self.calls.append(call)
        if call.path in self.overrides:
            return self.overrides[call.path](call)
        return self._route(call)

    def _route(self, call: Call) -> httpx.Response:
        m, p = call.method, call.path
        if (m, p) == ("POST", "/auth/login"):
            email = str(call.json["email"])
            known = USERS.get(email)
            if known is None or known[0] != call.json["password"]:
                return self._problem(401, "Неверный email или пароль")
            return httpx.Response(200, json=self._issue(email))
        if (m, p) == ("POST", "/auth/register"):
            if call.json["email"] in USERS:
                return self._problem(409, "Пользователь с таким email уже существует")
            if len(call.json["password"]) < 8:
                return self._problem(
                    422,
                    "Ошибка валидации",
                    errors=[{"loc": ["body", "password"], "msg": "слишком короткий", "type": "x"}],
                )
            USERS[call.json["email"]] = (call.json["password"], "customer")
            return httpx.Response(201, json={"id": str(uuid.uuid4()), "email": call.json["email"]})
        if (m, p) == ("POST", "/auth/refresh"):
            self.refresh_count += 1
            if not self.refresh_valid:
                return self._problem(401, "Недействительный refresh-токен")
            email = str(call.json["refresh_token"]).split("-", 2)[2]
            return httpx.Response(200, json=self._issue(email))
        if (m, p) == ("POST", "/auth/logout"):
            return httpx.Response(204)
        if (m, p) == ("GET", "/users/me"):
            me = self._user(call)
            if me is None:
                return self._problem(401, "Требуется авторизация")
            return httpx.Response(200, json={"id": "u-" + me, "email": me, "role": USERS[me][1]})
        if (m, p) == ("GET", "/health/live"):
            return httpx.Response(200, json={"status": "ok"})
        if (m, p) == ("GET", "/catalog/manufacturers"):
            return httpx.Response(
                200,
                json=[
                    {"id": 1, "name": "Bosch", "slug": "bosch"},
                    {"id": 2, "name": "Denso", "slug": "denso"},
                ],
            )
        if (m, p) == ("GET", "/catalog/injectors"):
            return self._catalog_list(call)
        if m == "GET" and (found := re.fullmatch(r"/catalog/injectors/(\d+)", p)):
            item = INJECTORS.get(int(found.group(1)))
            if item is None or not item["is_active"]:
                return self._problem(404, "Товар не найден")
            return httpx.Response(200, json=item)
        if (m, p) == ("POST", "/repair-requests"):
            if call.json.get("name") == "Бот":
                return self._problem(429, "Слишком много попыток, повторите позже")
            if not call.json.get("phone") and not call.json.get("email"):
                return self._problem(
                    422,
                    "Ошибка валидации",
                    errors=[
                        {"loc": ["body", "phone"], "msg": "Укажите телефон или email", "type": "x"}
                    ],
                )
            return httpx.Response(201, json={"id": str(uuid.uuid4()), "status": "NEW"})
        # Дальше только для вошедших.
        user = self._user(call)
        if user is None:
            return self._problem(401, "Требуется авторизация")
        return self._authorized(call, user)

    def _catalog_list(self, call: Call) -> httpx.Response:
        items = [i for i in INJECTORS.values() if i["is_active"]]
        if call.params.get("manufacturer"):
            items = [i for i in items if i["manufacturer"]["slug"] == call.params["manufacturer"]]
        if call.params.get("cursor") == "bad":
            return self._problem(422, "Некорректный курсор")
        next_cursor = (
            "c2" if call.params.get("limit") == "1" and not call.params.get("cursor") else None
        )
        return httpx.Response(200, json={"items": items, "next_cursor": next_cursor})

    def _authorized(self, call: Call, email: str) -> httpx.Response:
        m, p = call.method, call.path
        cart = self.carts.setdefault(email, {})
        if (m, p) == ("GET", "/cart"):
            return httpx.Response(200, json=self._cart_out(cart))
        if m == "PUT" and (found := re.fullmatch(r"/cart/items/(\d+)", p)):
            injector_id = int(found.group(1))
            quantity = int(call.json["quantity"])
            item = INJECTORS.get(injector_id)
            if item is None or quantity > item["stock"]:
                return self._problem(409, "Недостаточно товара на складе")
            cart[injector_id] = quantity
            return httpx.Response(200, json=self._cart_out(cart))
        if m == "DELETE" and (found := re.fullmatch(r"/cart/items/(\d+)", p)):
            cart.pop(int(found.group(1)), None)
            return httpx.Response(204)
        if (m, p) == ("POST", "/orders"):
            if not cart:
                return self._problem(409, "Корзина пуста")
            order_id = str(uuid.uuid5(uuid.NAMESPACE_URL, call.headers["idempotency-key"]))
            order = self._order(order_id, cart)
            self.orders[order_id] = order
            cart.clear()
            return httpx.Response(201, json=order)
        if (m, p) == ("GET", "/orders"):
            return httpx.Response(
                200, json={"items": list(self.orders.values()), "next_cursor": None}
            )
        if m == "GET" and (found := re.fullmatch(r"/orders/([0-9a-f-]+)", p)):
            known_order = self.orders.get(found.group(1))
            if known_order is None:
                return self._problem(404, "Заказ не найден")
            return httpx.Response(200, json={**known_order, "status": self.order_status})
        if m == "GET" and (found := re.fullmatch(r"/orders/([0-9a-f-]+)/payment", p)):
            return httpx.Response(200, json=self.payment)
        if m == "POST" and (found := re.fullmatch(r"/orders/([0-9a-f-]+)/cancel", p)):
            if self.order_status != "PENDING_PAYMENT":
                return self._problem(409, "Недопустимый переход")
            self.order_status = "CANCELLED"
            return httpx.Response(200, json=self.orders[found.group(1)])
        if p.startswith("/admin/catalog") or (m == "PUT" and p.endswith("/image")):
            return self._admin(call, email)
        return self._problem(404, "Не найдено")

    def _cart_out(self, cart: dict[int, int]) -> dict[str, Any]:
        lines = []
        for injector_id, quantity in cart.items():
            item = INJECTORS[injector_id]
            lines.append(
                {
                    "injector_id": injector_id,
                    "oem_number": item["oem_number"],
                    "title": item["title"],
                    "unit_price_kopecks": item["price_kopecks"],
                    "quantity": quantity,
                    "line_total_kopecks": item["price_kopecks"] * quantity,
                    "stock": item["stock"],
                    "available": quantity <= item["stock"],
                }
            )
        return {
            "items": lines,
            "total_kopecks": sum(line["line_total_kopecks"] for line in lines),
            "can_checkout": bool(lines),
        }

    def _order(self, order_id: str, cart: dict[int, int]) -> dict[str, Any]:
        out = self._cart_out(cart)
        return {
            "id": order_id,
            "status": "PENDING_PAYMENT",
            "total_kopecks": out["total_kopecks"],
            "currency": "RUB",
            "created_at": "2026-11-10T10:00:00Z",
            "expires_at": "2026-11-10T11:00:00Z",
            "paid_at": None,
            "items": [
                {
                    "injector_id": line["injector_id"],
                    "oem_number": line["oem_number"],
                    "title": line["title"],
                    "unit_price_kopecks": line["unit_price_kopecks"],
                    "quantity": line["quantity"],
                    "line_total_kopecks": line["line_total_kopecks"],
                }
                for line in out["items"]
            ],
        }

    def _admin(self, call: Call, email: str) -> httpx.Response:
        if USERS[email][1] != "admin":
            return self._problem(403, "Недостаточно прав")
        m, p = call.method, call.path
        if (m, p) == ("GET", "/admin/catalog/injectors"):
            return httpx.Response(
                200, json={"items": list(INJECTORS.values()), "next_cursor": None}
            )
        if m == "GET" and (found := re.fullmatch(r"/admin/catalog/injectors/(\d+)", p)):
            return httpx.Response(200, json=INJECTORS[int(found.group(1))])
        if (m, p) == ("POST", "/admin/catalog/injectors"):
            if call.json["oem_number"] == "DUP-001":
                return self._problem(409, "Товар с таким артикулом и состоянием уже существует")
            return httpx.Response(201, json={**INJECTORS[1], "id": 99, **call.json})
        if m == "PATCH":
            return httpx.Response(200, json={**INJECTORS[1], **call.json})
        if m == "PUT":
            return httpx.Response(200, json=INJECTORS[1])
        return self._problem(404, "Не найдено")
