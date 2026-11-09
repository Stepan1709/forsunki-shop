"""Сидирование каталога демонстрационными данными.

Данные РЕАЛИСТИЧНЫЕ ПО ФОРМЕ (настоящие производители, форматы артикулов, названия двигателей, диапазоны цен),
но случайные: применимость и цены не сверялись с каталогами. Для витрины и нагрузочных замеров это подходит,
для продажи реальным клиентам нужно заменить своими данными (через админку).
Генерация детерминирована (фиксированный seed): один и тот же запуск даёт одни и те же данные.
"""

import random
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import (
    Engine,
    Injector,
    InjectorCondition,
    InjectorType,
    Manufacturer,
)
from app.modules.catalog.repository import normalize_oem

MANUFACTURERS = [
    ("Bosch", "bosch"),
    ("Denso", "denso"),
    ("Delphi", "delphi"),
    ("Siemens", "siemens"),
]

COMMON_RAIL_ENGINES = [
    "Mercedes-Benz OM611 2.2 CDI",
    "Mercedes-Benz OM646 2.2 CDI",
    "Mercedes-Benz OM642 3.0 CDI",
    "Mercedes-Benz OM651 2.1 CDI",
    "Mercedes-Benz OM654 2.0 CDI",
    "VW CBAB 2.0 TDI CR",
    "VW CFFA 2.0 TDI CR",
    "VW CASA 3.0 TDI",
    "VW CAYC 1.6 TDI",
    "VW DFGA 2.0 TDI",
    "BMW N47 2.0d",
    "BMW B47 2.0d",
    "BMW M57 3.0d",
    "BMW N57 3.0d",
    "Ford Duratorq 1.8 TDCi",
    "Ford Duratorq 2.0 TDCi",
    "Ford Puma 2.2 TDCi",
    "Renault K9K 1.5 dCi",
    "Renault M9R 2.0 dCi",
    "Renault R9M 1.6 dCi",
    "Peugeot DV6 1.6 HDi",
    "Peugeot DW10 2.0 HDi",
    "Peugeot DW12 2.2 HDi",
    "Toyota 1KD-FTV 3.0 D-4D",
    "Toyota 2AD-FTV 2.2 D-4D",
    "Toyota 1ND-TV 1.4 D-4D",
    "Hyundai D4EB 2.2 CRDi",
    "Hyundai D4HB 2.2 CRDi",
    "Kia D4FB 1.6 CRDi",
    "Opel A20DTH 2.0 CDTI",
    "Opel Z19DT 1.9 CDTI",
    "Fiat 199A2 1.3 Multijet",
    "Fiat 2.0 Multijet",
    "Volvo D5244T 2.4 D5",
    "Mitsubishi 4N15 2.4 DI-D",
]
PUMP_INJECTOR_ENGINES = ["VW BKC 1.9 TDI PD", "VW BMM 2.0 TDI PD", "VW BXE 1.9 TDI PD"]


@dataclass(frozen=True)
class _Series:
    manufacturer: str
    type: InjectorType
    price_rub: tuple[int, int]  # диапазон цены новой форсунки, рубли


# Форматы артикулов: Bosch "0 445 110 183" (Common Rail) и "0 414 720 xxx" (насос-форсунки),
# Denso "095000-5550", Delphi "EJBR0xxxxD", Siemens "A2C59513554" / "5WS40xxx-Z".
SERIES = [
    _Series("bosch", InjectorType.COMMON_RAIL, (18_000, 65_000)),
    _Series("denso", InjectorType.COMMON_RAIL, (22_000, 70_000)),
    _Series("delphi", InjectorType.COMMON_RAIL, (15_000, 48_000)),
    _Series("siemens", InjectorType.COMMON_RAIL, (25_000, 60_000)),
    _Series("bosch", InjectorType.PUMP_INJECTOR, (12_000, 30_000)),
    _Series("delphi", InjectorType.PUMP_INJECTOR, (11_000, 26_000)),
]


def _oem(series: _Series, rng: random.Random) -> str:
    if series.manufacturer == "bosch" and series.type == InjectorType.COMMON_RAIL:
        return f"0 445 {rng.choice(['110', '115', '116', '120'])} {rng.randint(0, 999):03d}"
    if series.manufacturer == "bosch":
        return f"0 414 720 {rng.randint(0, 999):03d}"
    if series.manufacturer == "denso":
        return f"095000-{rng.randint(5000, 9999)}"
    if series.manufacturer == "delphi" and series.type == InjectorType.COMMON_RAIL:
        return f"EJBR0{rng.randint(1000, 5999)}{rng.choice('DZ')}"
    if series.manufacturer == "delphi":
        return f"BEBE4D{rng.randint(10000, 99999)}"
    if rng.random() < 0.5:
        return f"A2C{rng.randint(10_000_000, 99_999_999)}"
    return f"5WS40{rng.randint(100, 999)}-Z"


def _description(series: _Series, condition: InjectorCondition, oem: str) -> str:
    kind = "Common Rail" if series.type == InjectorType.COMMON_RAIL else "насос-форсунка"
    if condition == InjectorCondition.NEW:
        state = "Новая оригинальная форсунка, в заводской упаковке."
    else:
        state = (
            "Восстановленная форсунка: разобрана, дефектована, заменены изношенные детали, "
            "проверена на стенде."
        )
    return f"{state} Тип: {kind}. Артикул производителя {oem}. Гарантия 12 месяцев."


async def seed_catalog(session: AsyncSession, count: int, *, seed: int = 42) -> int:
    """Создаёт около `count` форсунок. Возвращает, сколько создано."""
    rng = random.Random(seed)  # noqa: S311  (детерминированный генератор для демо-данных, не криптография)

    manufacturers = {slug: Manufacturer(name=name, slug=slug) for name, slug in MANUFACTURERS}
    cr_engines = [Engine(name=n) for n in COMMON_RAIL_ENGINES]
    pd_engines = [Engine(name=n) for n in PUMP_INJECTOR_ENGINES]
    session.add_all([*manufacturers.values(), *cr_engines, *pd_engines])
    await session.flush()

    # Каждый артикул выпускается в двух состояниях (новая и восстановленная), поэтому артикулов вдвое меньше.
    articles = max(1, count // 2)
    seen: set[str] = set()
    created = 0
    batch: list[Injector] = []

    while len(seen) < articles:
        series = rng.choice(SERIES)
        oem = _oem(series, rng)
        normalized = normalize_oem(oem)
        if normalized in seen:
            continue
        seen.add(normalized)

        engine_pool = cr_engines if series.type == InjectorType.COMMON_RAIL else pd_engines
        engines = rng.sample(engine_pool, k=min(len(engine_pool), rng.randint(1, 4)))
        new_price = rng.randint(*series.price_rub) // 100 * 100  # круглые сотни рублей

        for condition, ratio in (
            (InjectorCondition.NEW, 1.0),
            (InjectorCondition.REMANUFACTURED, 0.6),
        ):
            if condition == InjectorCondition.REMANUFACTURED and rng.random() < 0.15:
                continue  # восстановленный вариант есть не у каждого артикула
            brand = series.manufacturer.capitalize()
            suffix = "" if condition == InjectorCondition.NEW else " (восстановленная)"
            batch.append(
                Injector(
                    oem_number=oem,
                    oem_normalized=normalized,
                    title=f"Форсунка {brand} {oem}{suffix}",
                    description=_description(series, condition, oem),
                    manufacturer_id=manufacturers[series.manufacturer].id,
                    type=series.type,
                    condition=condition,
                    price_kopecks=int(new_price * ratio) // 100 * 100 * 100,
                    stock=0 if rng.random() < 0.1 else rng.randint(1, 12),
                    engines=list(engines),
                )
            )
            created += 1
        if len(batch) >= 1000:
            session.add_all(batch)
            await session.flush()
            batch.clear()

    session.add_all(batch)
    await session.commit()
    return created


async def reset_catalog(session: AsyncSession) -> None:
    await session.execute(
        text(
            "TRUNCATE injector_engines, injectors, engines, manufacturers RESTART IDENTITY CASCADE"
        )
    )
    await session.commit()
