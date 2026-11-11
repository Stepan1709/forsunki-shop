"""Сайт ForsunkiShop: FastAPI + Jinja2 + HTMX. Работает как BFF: ходит в api по HTTP, токены держит в Redis."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app.clientip import ClientIpMiddleware
from app.config import Settings
from app.deps import csrf_protect
from app.errors import install_error_handlers
from app.logging import CorrelationIdMiddleware, configure_logging
from app.metrics import MetricsMiddleware, metrics_endpoint
from app.routes import admin, auth, cart, orders, pages, shop
from app.sessions import SessionMiddleware, SessionStore
from app.tracing import instrument_app, setup_tracing, shutdown_tracing

log = structlog.get_logger("web")
STATIC = Path(__file__).parent / "static"


def create_app(
    settings: Settings | None = None, api_transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    """`api_transport` нужен тестам: подмена бэкенда на уровне HTTP без сети."""
    settings = settings or Settings()  # type: ignore[call-arg]
    configure_logging(settings)
    setup_tracing(settings)

    # Ресурсы создаются один раз при старте (клиенты и пулы живут весь процесс) и закрываются при остановке.
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    store = SessionStore(redis, settings)
    http = httpx.AsyncClient(
        base_url=settings.api_url,
        timeout=settings.api_timeout_seconds,
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        transport=api_transport,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        log.info("web_started")
        yield
        log.info("web_stopping")
        await http.aclose()
        await redis.aclose()
        shutdown_tracing()

    app = FastAPI(
        title="ForsunkiShop", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.settings = settings
    app.state.redis = redis
    app.state.session_store = store
    app.state.http = http

    # Порядок: внешний (последний добавленный) выполняется первым. Сначала correlation_id, затем сессия.
    app.add_middleware(SessionMiddleware, store=store, settings=settings)
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(ClientIpMiddleware, trusted_proxies=settings.trusted_proxies)

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")
    app.include_router(pages.health_router)
    # Метрики забирает Prometheus внутри сети; наружу (nginx) путь не публикуется.
    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
    # CSRF-защита на всех страницах с формами; безопасные методы (GET) она пропускает.
    protected = [Depends(csrf_protect)]
    for router in (
        pages.router,
        auth.router,
        shop.router,
        cart.router,
        orders.router,
        admin.router,
    ):
        app.include_router(router, dependencies=protected)
    install_error_handlers(app)
    instrument_app(app)  # после middleware и роутеров: трасса охватывает весь запрос
    return app
