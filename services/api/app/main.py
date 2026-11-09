from fastapi import FastAPI
from starlette.types import Lifespan

from app.core import debug, health
from app.core.config import Settings
from app.core.correlation import CorrelationIdMiddleware
from app.core.errors import install_error_handlers
from app.core.lifespan import resources_lifespan
from app.core.logging import configure_logging


def create_app(
    settings: Settings | None = None, lifespan: Lifespan[FastAPI] | None = None
) -> FastAPI:
    """Фабрика приложения. Настройки и lifespan можно подменить в тестах."""
    settings = settings or Settings()  # type: ignore[call-arg]  # пароли читаются из секретов
    configure_logging(settings)

    app = FastAPI(
        title="ForsunkiShop API",
        version="0.1.0",
        lifespan=lifespan or resources_lifespan,
    )
    app.state.settings = settings

    app.add_middleware(CorrelationIdMiddleware)
    install_error_handlers(app)

    app.include_router(health.router)
    if settings.env != "prod":
        app.include_router(debug.router)
    return app
