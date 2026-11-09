from fastapi import APIRouter, Depends, Response, status

from app.modules.users.deps import get_auth_service, get_client_ip, get_current_user
from app.modules.users.models import User
from app.modules.users.schemas import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from app.modules.users.service import AuthService

auth_router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])


@auth_router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(
    body: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
    client_ip: str = Depends(get_client_ip),
) -> User:
    return await service.register(body.email, body.password, client_ip)


@auth_router.post("/login", response_model=TokenPair)
async def login(
    body: LoginRequest,
    service: AuthService = Depends(get_auth_service),
    client_ip: str = Depends(get_client_ip),
) -> TokenPair:
    return await service.login(body.email, body.password, client_ip)


@auth_router.post("/refresh", response_model=TokenPair)
async def refresh(
    body: RefreshRequest, service: AuthService = Depends(get_auth_service)
) -> TokenPair:
    return await service.refresh(body.refresh_token)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    body: RefreshRequest, service: AuthService = Depends(get_auth_service)
) -> Response:
    await service.logout(body.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@users_router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
