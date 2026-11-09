from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import get_redis, get_session, get_settings
from app.core.events import EventSink, get_event_sink
from app.core.idempotency import get_idempotency_key
from app.core.ratelimit import RateLimiter
from app.modules.repair.schemas import RepairRequestIn, RepairRequestOut
from app.modules.repair.service import RepairService
from app.modules.users.deps import get_client_ip

router = APIRouter(prefix="/repair-requests", tags=["repair"])


def get_repair_service(
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    redis: Redis = Depends(get_redis),
    events: EventSink = Depends(get_event_sink),
) -> RepairService:
    return RepairService(session, settings, RateLimiter(redis), events)


@router.post("", response_model=RepairRequestOut, status_code=201)
async def create_repair_request(
    body: RepairRequestIn,
    key: str = Depends(get_idempotency_key),
    client_ip: str = Depends(get_client_ip),
    service: RepairService = Depends(get_repair_service),
) -> JSONResponse:
    """Заявка на ремонт (доступна без входа). Повтор с тем же Idempotency-Key вернёт ту же заявку (200)."""
    status, payload = await service.submit(body, key, client_ip)
    headers = {"Idempotent-Replayed": "true"} if status == 200 else {}
    return JSONResponse(payload, status_code=status, headers=headers)
