from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.modules.cart.schemas import CartOut, SetQuantityRequest
from app.modules.cart.service import CartService
from app.modules.users.deps import get_current_user
from app.modules.users.models import User

router = APIRouter(prefix="/cart", tags=["cart"])


def get_cart_service(session: AsyncSession = Depends(get_session)) -> CartService:
    return CartService(session)


@router.get("", response_model=CartOut)
async def get_cart(
    user: User = Depends(get_current_user), service: CartService = Depends(get_cart_service)
) -> CartOut:
    return await service.view(user.id)


# PUT, а не POST: «установить количество N» естественно идемпотентно, повтор запроса даёт тот же результат,
# поэтому ключ идемпотентности здесь не нужен.
@router.put("/items/{injector_id}", response_model=CartOut)
async def set_quantity(
    injector_id: int,
    body: SetQuantityRequest,
    user: User = Depends(get_current_user),
    service: CartService = Depends(get_cart_service),
) -> CartOut:
    return await service.set_quantity(user.id, injector_id, body.quantity)


@router.delete("/items/{injector_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_item(
    injector_id: int,
    user: User = Depends(get_current_user),
    service: CartService = Depends(get_cart_service),
) -> Response:
    await service.remove(user.id, injector_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def clear_cart(
    user: User = Depends(get_current_user), service: CartService = Depends(get_cart_service)
) -> Response:
    await service.clear(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
