from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import get_redis, get_session, get_settings
from app.core.errors import ForbiddenError, UnauthorizedError
from app.core.events import EventSink, get_event_sink
from app.core.ratelimit import RateLimiter
from app.core.security import TokenError, decode_access_token
from app.modules.users.models import User, UserRole
from app.modules.users.repository import UserRepository
from app.modules.users.service import AuthService

# auto_error=False: ошибку формируем сами в формате Problem Details.
_bearer = HTTPBearer(auto_error=False)


def get_client_ip(request: Request) -> str:
    # За nginx здесь будет адрес прокси; реальный IP придёт из заголовка после настройки
    # доверенных прокси. X-Forwarded-For без этого доверять нельзя: его подделывают.
    return request.client.host if request.client else "unknown"


def get_auth_service(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
    events: EventSink = Depends(get_event_sink),
) -> AuthService:
    return AuthService(session, settings, RateLimiter(redis), events)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    if credentials is None:
        raise UnauthorizedError()
    try:
        claims = decode_access_token(credentials.credentials, settings)
    except TokenError:
        raise UnauthorizedError("Недействительный или просроченный токен") from None
    # Пользователя берём из БД, а не доверяем роли из токена: блокировка и смена роли
    # действуют сразу, а не после истечения access-токена.
    user = await UserRepository(session).get_by_id(claims.user_id)
    if user is None or not user.is_active:
        raise UnauthorizedError("Недействительный или просроченный токен")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != UserRole.ADMIN:
        raise ForbiddenError()
    return user
