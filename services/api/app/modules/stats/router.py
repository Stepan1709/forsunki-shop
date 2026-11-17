from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_session
from app.modules.stats.schemas import Group, StatsReport
from app.modules.stats.service import StatsService
from app.modules.users.deps import require_admin

# Статистика только для администратора: роль проверяется на уровне всего роутера.
router = APIRouter(prefix="/admin/stats", tags=["admin"], dependencies=[Depends(require_admin)])


def get_stats_service(session: AsyncSession = Depends(get_session)) -> StatsService:
    return StatsService(session)


@router.get("", response_model=StatsReport)
async def report(
    date_from: date | None = Query(
        None, alias="from", description="первый день периода (ГГГГ-ММ-ДД)"
    ),
    date_to: date | None = Query(
        None, alias="to", description="последний день периода включительно"
    ),
    group: Group = Query("day", description="группировка точек графика: day, week или month"),
    top: int = Query(10, ge=1, le=50, description="сколько товаров показывать в рейтингах"),
    service: StatsService = Depends(get_stats_service),
) -> StatsReport:
    return await service.report(date_from, date_to, group, top)
