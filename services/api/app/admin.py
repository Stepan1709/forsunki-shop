"""Админ-панель на SQLAdmin (/admin): управление производителями, двигателями и форсунками.

Вход по тем же пользователям, что и в API, но только с ролью admin. Проверка роли делается заново на
каждый запрос к панели (а не один раз при входе): разжалованный администратор теряет доступ сразу.
Любое изменение товара сбрасывает кэш каталога (увеличивается его версия).
"""

import hashlib
import hmac
import uuid
from typing import Any

import structlog
from fastapi import FastAPI
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from app.core.cache import Cache
from app.core.config import Settings
from app.core.lifespan import Resources
from app.core.ratelimit import RateLimiter
from app.core.security import DUMMY_HASH, verify_password
from app.modules.catalog.models import Engine, Injector, Manufacturer
from app.modules.catalog.repository import normalize_oem
from app.modules.users.models import UserRole
from app.modules.users.repository import UserRepository

log = structlog.get_logger("admin")

_SESSION_KEY = "admin_user_id"


class AdminAuth(AuthenticationBackend):
    def __init__(self, app: FastAPI, secret_key: str) -> None:
        super().__init__(secret_key=secret_key)
        self._app = app

    @property
    def _resources(self) -> Resources:
        resources: Resources = self._app.state.resources
        return resources

    async def login(self, request: Request) -> bool:
        form = await request.form()
        email = str(form.get("username", "")).strip().lower()
        password = str(form.get("password", ""))
        settings: Settings = self._app.state.settings

        # Тот же ограничитель, что и у API: форма входа в админку не должна быть лазейкой для перебора.
        limiter = RateLimiter(self._resources.redis)
        ip = request.client.host if request.client else "unknown"
        await limiter.check(
            f"admin_login:ip:{ip}", settings.login_ip_limit, settings.login_ip_window_seconds
        )

        async with self._resources.session_factory() as session:
            user = await UserRepository(session).get_by_email(email)
        # Пароль проверяем всегда (против фиктивного хэша, если пользователя нет): время ответа одинаково.
        password_ok = await verify_password(user.password_hash if user else DUMMY_HASH, password)
        if user is None or not password_ok or not user.is_active or user.role != UserRole.ADMIN:
            log.warning("admin_login_failed")
            return False
        request.session[_SESSION_KEY] = str(user.id)
        log.info("admin_login_succeeded", user_id=str(user.id))
        return True

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        raw_id = request.session.get(_SESSION_KEY)
        if not raw_id:
            return False

        try:
            user_id = uuid.UUID(raw_id)
        except ValueError:
            return False
        async with self._resources.session_factory() as session:
            user = await UserRepository(session).get_by_id(user_id)
        return user is not None and user.is_active and user.role == UserRole.ADMIN


class _CatalogView(ModelView):
    """Общее поведение: после любого изменения сбрасываем кэш каталога."""

    can_export = False
    # Основное приложение (запрос к панели приходит во вложенное приложение SQLAdmin, у которого
    # нет наших ресурсов). Заполняется в setup_admin.
    main_app: FastAPI

    async def _invalidate(self, request: Request) -> None:
        resources: Resources = self.main_app.state.resources
        await Cache(resources.redis).bump_version()

    async def after_model_change(
        self, data: dict[str, Any], model: Any, is_created: bool, request: Request
    ) -> None:
        await self._invalidate(request)

    async def after_model_delete(self, model: Any, request: Request) -> None:
        await self._invalidate(request)


class ManufacturerAdmin(_CatalogView, model=Manufacturer):
    name = "Производитель"
    name_plural = "Производители"
    icon = "fa-solid fa-industry"
    column_list = [Manufacturer.id, Manufacturer.name, Manufacturer.slug]
    column_searchable_list = [Manufacturer.name]
    form_columns = [Manufacturer.name, Manufacturer.slug]


class EngineAdmin(_CatalogView, model=Engine):
    name = "Двигатель"
    name_plural = "Двигатели"
    icon = "fa-solid fa-gears"
    column_list = [Engine.id, Engine.name]
    column_searchable_list = [Engine.name]
    form_columns = [Engine.name]


class InjectorAdmin(_CatalogView, model=Injector):
    name = "Форсунка"
    name_plural = "Форсунки"
    icon = "fa-solid fa-oil-can"
    column_list = [
        Injector.id,
        Injector.oem_number,
        Injector.title,
        Injector.manufacturer,
        Injector.type,
        Injector.condition,
        Injector.price_kopecks,
        Injector.stock,
        Injector.is_active,
    ]
    column_searchable_list = [Injector.oem_number, Injector.title]
    column_sortable_list = [Injector.id, Injector.price_kopecks, Injector.stock]
    column_labels = {Injector.price_kopecks: "Цена, коп."}
    # oem_normalized и image_key в форме нет: первый вычисляется сам, картинку загружают через API.
    form_columns = [
        Injector.oem_number,
        Injector.title,
        Injector.description,
        Injector.manufacturer,
        Injector.type,
        Injector.condition,
        Injector.price_kopecks,
        Injector.stock,
        Injector.is_active,
        Injector.engines,
    ]

    async def on_model_change(
        self, data: dict[str, Any], model: Any, is_created: bool, request: Request
    ) -> None:
        # Нормализованный артикул вычисляем из того, что ввёл администратор: поиск не должен
        # зависеть от того, поставил ли он пробелы.
        if "oem_number" in data:
            data["oem_normalized"] = normalize_oem(str(data["oem_number"]))


def setup_admin(app: FastAPI) -> Admin:
    settings: Settings = app.state.settings
    resources: Resources = app.state.resources
    # Ключ подписи cookie сессии админки выводим из jwt_secret (HMAC с «солью-назначением»),
    # чтобы не заводить ещё один секрет и чтобы ключи разных назначений отличались.
    secret = hmac.new(
        settings.jwt_secret.get_secret_value().encode(), b"sqladmin-session", hashlib.sha256
    ).hexdigest()
    admin = Admin(
        app,
        engine=resources.engine,
        authentication_backend=AdminAuth(app, secret),
        title="ForsunkiShop: админка",
        base_url="/admin",
    )
    _CatalogView.main_app = app
    for view in (ManufacturerAdmin, EngineAdmin, InjectorAdmin):
        admin.add_view(view)
    return admin
