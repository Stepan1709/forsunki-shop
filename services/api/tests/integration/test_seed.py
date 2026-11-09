from fastapi import FastAPI
from sqlalchemy import func, select

from app.modules.catalog.models import Engine, Injector, InjectorCondition, InjectorType
from app.modules.catalog.repository import normalize_oem
from app.seed import reset_catalog, seed_catalog


async def test_seed_creates_consistent_catalog(app: FastAPI) -> None:
    async with app.state.resources.session_factory() as session:
        created = await seed_catalog(session, 300)

        injectors = list((await session.execute(select(Injector))).scalars().unique())
        engines = (await session.execute(select(func.count(Engine.id)))).scalar_one()

    assert created == len(injectors)
    assert 250 <= created <= 300
    assert engines > 30
    # Нормализованный артикул совпадает с исходным: поиск найдёт то, что показано в каталоге.
    assert all(i.oem_normalized == normalize_oem(i.oem_number) for i in injectors)
    # У каждой форсунки есть применимость, у насос-форсунок свои двигатели, цены и остатки корректны.
    assert all(i.engines for i in injectors)
    assert all(i.price_kopecks > 0 and i.stock >= 0 for i in injectors)
    assert {i.condition for i in injectors} == set(InjectorCondition)
    assert {i.type for i in injectors} == set(InjectorType)
    reman = [i for i in injectors if i.condition == InjectorCondition.REMANUFACTURED]
    assert reman  # восстановленные дешевле новых аналогов, но не бесплатны
    assert all(i.price_kopecks > 0 for i in reman)


async def test_seed_is_deterministic_and_reset_clears_data(app: FastAPI) -> None:
    async with app.state.resources.session_factory() as session:
        await seed_catalog(session, 100)
        first = sorted(
            (await session.execute(select(Injector.oem_normalized, Injector.condition))).all()
        )
        await reset_catalog(session)
        assert (await session.execute(select(func.count(Injector.id)))).scalar_one() == 0

        await seed_catalog(session, 100)
        second = sorted(
            (await session.execute(select(Injector.oem_normalized, Injector.condition))).all()
        )

    assert first == second  # фиксированный seed: один и тот же запуск даёт одни и те же данные
