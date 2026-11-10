"""Зависимости FastAPI: сессия, клиент api, проверка входа и роли, защита от CSRF."""

import hmac
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request

from app.api_client import ApiClient, AuthRequired
from app.config import Settings
from app.sessions import Session, SessionStore


class Forbidden(Exception):
    """Вошёл, но прав недостаточно."""


class CsrfFailed(Exception):
    pass


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_session(request: Request) -> Session:
    session: Session = request.state.session
    return session


def get_api(request: Request, session: Annotated[Session, Depends(get_session)]) -> ApiClient:
    store: SessionStore = request.app.state.session_store
    return ApiClient(request.app.state.http, session, store)


SessionDep = Annotated[Session, Depends(get_session)]
ApiDep = Annotated[ApiClient, Depends(get_api)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    role: str

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def current_user(session: SessionDep) -> CurrentUser | None:
    user: dict[str, Any] | None = session.get("user")
    if not user or not session.is_authenticated:
        return None
    return CurrentUser(id=user["id"], email=user["email"], role=user["role"])


def require_user(user: Annotated[CurrentUser | None, Depends(current_user)]) -> CurrentUser:
    if user is None:
        raise AuthRequired
    return user


def require_admin(user: Annotated[CurrentUser, Depends(require_user)]) -> CurrentUser:
    # Это удобство интерфейса. Настоящая проверка роли выполняется в api на каждый запрос.
    if not user.is_admin:
        raise Forbidden
    return user


UserDep = Annotated[CurrentUser, Depends(require_user)]
MaybeUserDep = Annotated[CurrentUser | None, Depends(current_user)]
AdminDep = Annotated[CurrentUser, Depends(require_admin)]


async def csrf_protect(request: Request, session: SessionDep) -> None:
    """Небезопасные методы принимаются только с токеном сессии: из скрытого поля формы или заголовка HTMX.

    Без этого чужая страница могла бы отправить браузеру пользователя POST на наш сайт, а браузер приложил бы
    к нему cookie сессии (CSRF). Токен чужая страница узнать не может. Сравнение за постоянное время.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied:
        form = await request.form()
        value = form.get("csrf_token")
        supplied = value if isinstance(value, str) else None
    if not supplied or not hmac.compare_digest(supplied, session.csrf_token):
        raise CsrfFailed
