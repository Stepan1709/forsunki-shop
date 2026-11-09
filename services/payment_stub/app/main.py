"""Заглушка платёжного провайдера.

Имитирует внешнюю платёжную систему: принимает запрос на создание платежа, показывает покупателю страницу
«Оплатить / Отклонить» и по результату шлёт магазину подписанный webhook. Заменяется настоящей онлайн-кассой без
изменения бизнес-кода магазина (он знает только интерфейс PaymentGateway).
"""

import asyncio
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
import structlog
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.types import Lifespan

from app.config import Settings
from app.logging import configure_logging
from app.storage import Payment, Storage
from app.webhooks import deliver_once, deliver_with_retries, redeliver_loop

log = structlog.get_logger("stub")
BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def _rub(kopecks: int) -> str:
    """45000 копеек -> '450,00' (разделитель тысяч: неразрывный пробел)."""
    rubles, cents = divmod(kopecks, 100)
    return f"{rubles:,}".replace(",", " ") + f",{cents:02d}"


templates.env.filters["rub"] = _rub


class CreatePayment(BaseModel):
    order_id: str = Field(min_length=1, max_length=64)
    amount_kopecks: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    description: str = Field(default="Оплата заказа", max_length=200)
    return_url: str = Field(min_length=1, max_length=500)


def make_lifespan(settings: Settings, client: httpx.AsyncClient | None) -> Lifespan[FastAPI]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        storage = Storage(settings.database_path)
        http = client or httpx.AsyncClient(timeout=10)
        stop = asyncio.Event()
        app.state.storage = storage
        app.state.http = http
        app.state.tasks = (
            set()
        )  # ссылки на фоновые задачи: без них сборщик мусора мог бы их оборвать
        redeliver = asyncio.create_task(redeliver_loop(settings, http, storage, stop))
        try:
            yield
        finally:
            stop.set()
            await redeliver
            for task in list(app.state.tasks):
                task.cancel()
            if client is None:
                await http.aclose()
            storage.close()

    return lifespan


def create_app(
    settings: Settings | None = None, client: httpx.AsyncClient | None = None
) -> FastAPI:
    settings = settings or Settings()  # type: ignore[call-arg]
    configure_logging(settings)
    app = FastAPI(title="ForsunkiShop Payment Stub", lifespan=make_lifespan(settings, client))
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    def storage(request: Request) -> Storage:
        return request.app.state.storage  # type: ignore[no-any-return]

    def require_api_key(authorization: Annotated[str | None, Header()] = None) -> None:
        expected = f"Bearer {settings.payment_api_key.get_secret_value()}"
        # compare_digest: сравнение за постоянное время, по скорости ответа ключ не подобрать.
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(status_code=401, detail="Неверный ключ API")

    def schedule(request: Request, background: BackgroundTasks, payment: Payment) -> None:
        background.add_task(
            deliver_with_retries,
            settings,
            request.app.state.http,
            request.app.state.storage,
            payment,
        )

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------ API для магазина
    @app.post("/payments", dependencies=[Depends(require_api_key)])
    async def create_payment(
        body: CreatePayment,
        store: Annotated[Storage, Depends(storage)],
        idempotency_key: Annotated[str | None, Header()] = None,
        x_request_id: Annotated[str | None, Header()] = None,
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(status_code=400, detail="Нужен заголовок Idempotency-Key")
        payment, created = await store.create_or_get(
            {
                "idempotency_key": idempotency_key,
                "order_id": body.order_id,
                "amount_kopecks": body.amount_kopecks,
                "currency": body.currency,
                "description": body.description,
                "return_url": body.return_url,
                "correlation_id": x_request_id,
            }
        )
        if not created and (
            payment.order_id != body.order_id or payment.amount_kopecks != body.amount_kopecks
        ):
            # Так ведут себя настоящие провайдеры: тот же ключ с другими параметрами это ошибка клиента.
            raise HTTPException(status_code=409, detail="Idempotency-Key уже использован")
        return JSONResponse(
            {
                "payment_id": payment.id,
                "pay_url": f"{settings.public_base_url}/pay/{payment.id}",
                "status": payment.status,
            },
            status_code=201 if created else 200,
        )

    @app.get("/payments/{payment_id}", dependencies=[Depends(require_api_key)])
    async def get_payment(
        payment_id: str, store: Annotated[Storage, Depends(storage)]
    ) -> dict[str, Any]:
        payment = await store.get(payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Платёж не найден")
        return {"payment_id": payment.id, "status": payment.status, "order_id": payment.order_id}

    @app.post("/payments/{payment_id}/webhook/resend", dependencies=[Depends(require_api_key)])
    async def resend_webhook(
        payment_id: str, request: Request, store: Annotated[Storage, Depends(storage)]
    ) -> JSONResponse:
        """Повторно отправляет то же событие (тот же event_id): имитация дубля, который иногда шлют провайдеры."""
        payment = await store.get(payment_id)
        if payment is None or payment.status == "pending":
            raise HTTPException(status_code=409, detail="Платёж ещё не завершён")
        delivered = await deliver_once(settings, request.app.state.http, payment)
        return JSONResponse({"delivered": delivered})

    # ------------------------------------------------------------------ страница покупателя
    @app.get("/pay/{payment_id}", response_class=HTMLResponse)
    async def pay_page(
        payment_id: str, request: Request, store: Annotated[Storage, Depends(storage)]
    ) -> Response:
        payment = await store.get(payment_id)
        if payment is None:
            return templates.TemplateResponse(request, "not_found.html", status_code=404)
        return templates.TemplateResponse(request, "pay.html", {"payment": payment})

    async def finish(
        payment_id: str,
        status: str,
        request: Request,
        background: BackgroundTasks,
        store: Storage,
    ) -> RedirectResponse:
        if await store.get(payment_id) is None:
            raise HTTPException(status_code=404, detail="Платёж не найден")
        finalized = await store.finalize(payment_id, status)
        if finalized is not None:  # итог фиксируется один раз; повторный клик ничего не меняет
            log.info("payment_finalized", payment_id=payment_id, status=status)
            schedule(request, background, finalized)
        # 303: после POST браузер показывает страницу GET, обновление страницы не повторяет действие.
        return RedirectResponse(f"/pay/{payment_id}", status_code=303)

    @app.post("/pay/{payment_id}/confirm")
    async def confirm(
        payment_id: str,
        request: Request,
        background: BackgroundTasks,
        store: Annotated[Storage, Depends(storage)],
    ) -> RedirectResponse:
        return await finish(payment_id, "succeeded", request, background, store)

    @app.post("/pay/{payment_id}/decline")
    async def decline(
        payment_id: str,
        request: Request,
        background: BackgroundTasks,
        store: Annotated[Storage, Depends(storage)],
    ) -> RedirectResponse:
        return await finish(payment_id, "failed", request, background, store)

    return app
