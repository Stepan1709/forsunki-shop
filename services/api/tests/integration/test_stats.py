"""Статистика: отчёт для администратора, перенос счётчиков просмотров из Redis, чистка старых дней."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text

from app.core.config import Settings
from app.core.errors import AppError
from app.modules.orders.models import Order, OrderItem
from app.modules.orders.state import OrderStatus
from app.modules.stats.models import ProductViewsDaily, SiteStatsDaily
from app.modules.stats.service import MSK, bucket_start, next_bucket, validate_range
from app.worker.retention import RetentionCleaner
from app.worker.stats_flush import PRODUCT_PREFIX, VIEWS_PREFIX, StatsFlusher
from tests.integration.conftest import create_user

URL = "/admin/stats"


# ------------------------------------------------------------------------------------ вспомогательное
async def add_views(app: FastAPI, rows: dict[date, int]) -> None:
    async with app.state.resources.session_factory() as session:
        session.add_all(SiteStatsDaily(day=d, page_views=n) for d, n in rows.items())
        await session.commit()


async def add_product_views(app: FastAPI, rows: list[tuple[date, int, int]]) -> None:
    async with app.state.resources.session_factory() as session:
        session.add_all(ProductViewsDaily(day=d, injector_id=i, views=n) for d, i, n in rows)
        await session.commit()


async def add_order(
    app: FastAPI,
    user_id: uuid.UUID,
    injector_id: int,
    *,
    paid_at: datetime | None,
    quantity: int = 1,
    unit_price: int = 100_000,
    status: OrderStatus = OrderStatus.PAID,
) -> None:
    now = datetime.now(UTC)
    async with app.state.resources.session_factory() as session:
        order = Order(
            user_id=user_id,
            status=status,
            total_kopecks=quantity * unit_price,
            expires_at=now + timedelta(hours=1),
            paid_at=paid_at,
        )
        order.items = [
            OrderItem(
                injector_id=injector_id,
                oem_number=f"OEM-{injector_id}",
                title=f"Форсунка {injector_id}",
                unit_price_kopecks=unit_price,
                quantity=quantity,
            )
        ]
        session.add(order)
        await session.commit()


def msk(day: date, hour: int = 12) -> datetime:
    """Момент внутри дня по московскому времени (в БД хранится с часовым поясом)."""
    return datetime.combine(day, time(hour), tzinfo=MSK)


async def report(
    client: httpx.AsyncClient, headers: dict[str, str], **params: object
) -> dict[str, object]:
    response = await client.get(URL, params=params, headers=headers)  # type: ignore[arg-type]
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


# ------------------------------------------------------------------------------------ чистая логика
def test_bucket_helpers_align_to_day_week_and_month() -> None:
    wednesday = date(2026, 1, 14)
    assert bucket_start(wednesday, "day") == wednesday
    assert bucket_start(wednesday, "week") == date(2026, 1, 12)  # понедельник
    assert bucket_start(wednesday, "month") == date(2026, 1, 1)
    assert next_bucket(date(2026, 1, 12), "week") == date(2026, 1, 19)
    assert next_bucket(date(2026, 1, 31), "day") == date(2026, 2, 1)
    assert next_bucket(date(2026, 1, 1), "month") == date(2026, 2, 1)
    assert next_bucket(date(2026, 12, 1), "month") == date(2027, 1, 1)  # переход через год


def test_validate_range_defaults_and_limits() -> None:
    start, end = validate_range(None, None)
    assert (end - start).days == 29  # 30 дней включая сегодня
    with pytest.raises(AppError, match="позже"):
        validate_range(date(2026, 2, 1), date(2026, 1, 1))
    with pytest.raises(AppError, match="не больше"):
        validate_range(date(2024, 1, 1), date(2026, 1, 1))


# ------------------------------------------------------------------------------------ API отчёта
async def test_stats_api_requires_admin(
    client: httpx.AsyncClient, customer_headers: dict[str, str]
) -> None:
    assert (await client.get(URL)).status_code == 401
    assert (await client.get(URL, headers=customer_headers)).status_code == 403


async def test_stats_rejects_bad_parameters(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    for params in (
        {"group": "year"},
        {"top": 0},
        {"top": 51},
        {"from": "не-дата"},
        {"from": "2026-02-01", "to": "2026-01-01"},
        {"from": "2024-01-01", "to": "2026-01-01"},
    ):
        response = await client.get(URL, params=params, headers=admin_headers)
        assert response.status_code == 422, params


async def test_empty_report_has_a_zero_point_per_day(
    client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    body = await report(client, admin_headers, **{"from": "2026-03-01", "to": "2026-03-10"})
    points = body["points"]
    assert isinstance(points, list)
    assert [p["start"] for p in points] == [f"2026-03-{d:02d}" for d in range(1, 11)]
    assert all(
        p["page_views"] == p["registrations"] == p["orders"] == p["revenue_kopecks"] == 0
        for p in points
    )
    assert body["views_tracked_since"] is None
    assert body["top_viewed"] == []
    assert body["top_purchased"] == []


async def test_report_combines_views_registrations_orders_and_tops(
    app: FastAPI,
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    first, second = catalog["bosch_new"], catalog["denso_new"]
    buyer_id, _ = await create_user(app, "buyer@example.com")
    d1, d2, d3 = date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)

    await add_views(app, {d1: 100, d2: 50, date(2026, 3, 20): 999})  # последний день вне периода
    await add_product_views(app, [(d1, first, 7), (d2, first, 3), (d2, second, 12)])
    # Регистрации: два человека второго числа, один четвёртого (по московскому времени).
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(text("UPDATE users SET created_at = :ts"), {"ts": msk(d2)})
    await create_user(app, "second@example.com")
    async with app.state.resources.engine.begin() as conn:
        await conn.execute(
            text("UPDATE users SET created_at = :ts WHERE email = 'second@example.com'"),
            {"ts": msk(d3)},
        )
    # Покупки: два оплаченных заказа, неоплаченный и оплаченный вне периода в отчёт не попадают.
    await add_order(app, buyer_id, first, paid_at=msk(d2), quantity=2, unit_price=500_000)
    await add_order(app, buyer_id, second, paid_at=msk(d3), quantity=1, unit_price=300_000)
    await add_order(app, buyer_id, first, paid_at=None, status=OrderStatus.PENDING_PAYMENT)
    await add_order(app, buyer_id, first, paid_at=msk(date(2026, 4, 1)), quantity=5)

    body = await report(client, admin_headers, **{"from": "2026-03-01", "to": "2026-03-05"})

    assert (
        body["totals"]
        == {
            "page_views": 150,
            "product_views": 22,
            "registrations": 3,  # администратор и покупатель датированы вторым числом, ещё один четвёртым
            "orders": 2,
            "revenue_kopecks": 1_300_000,
        }
    )
    by_day = {p["start"]: p for p in body["points"]}  # type: ignore[attr-defined]
    assert by_day["2026-03-02"]["page_views"] == 100
    assert by_day["2026-03-03"]["registrations"] == 2
    assert by_day["2026-03-04"]["registrations"] == 1
    assert by_day["2026-03-03"]["orders"] == 1
    assert by_day["2026-03-03"]["revenue_kopecks"] == 1_000_000
    assert by_day["2026-03-04"]["revenue_kopecks"] == 300_000
    assert body["views_tracked_since"] == "2026-03-02"

    viewed = body["top_viewed"]
    assert [(v["injector_id"], v["views"]) for v in viewed] == [(second, 12), (first, 10)]  # type: ignore[attr-defined]
    purchased = body["top_purchased"]
    assert [(p["injector_id"], p["quantity"], p["revenue_kopecks"]) for p in purchased] == [  # type: ignore[attr-defined]
        (first, 2, 1_000_000),
        (second, 1, 300_000),
    ]
    assert purchased[0]["title"] == f"Форсунка {first}"  # type: ignore[index]


async def test_orders_are_attributed_by_payment_day_in_moscow_time(
    app: FastAPI,
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    buyer_id, _ = await create_user(app, "late@example.com")
    # 21:30 UTC 10 января это уже 00:30 11 января в Москве: заказ относится к 11-му.
    await add_order(
        app, buyer_id, catalog["bosch_new"], paid_at=datetime(2026, 1, 10, 21, 30, tzinfo=UTC)
    )
    body = await report(client, admin_headers, **{"from": "2026-01-10", "to": "2026-01-11"})
    by_day = {p["start"]: p["orders"] for p in body["points"]}  # type: ignore[attr-defined]
    assert by_day == {"2026-01-10": 0, "2026-01-11": 1}


async def test_group_by_week_and_month(
    app: FastAPI, client: httpx.AsyncClient, admin_headers: dict[str, str]
) -> None:
    await add_views(
        app,
        {
            date(2026, 1, 5): 10,  # понедельник
            date(2026, 1, 11): 5,  # воскресенье той же недели
            date(2026, 1, 12): 7,  # следующая неделя
            date(2026, 2, 1): 20,
        },
    )
    weekly = await report(
        client, admin_headers, group="week", **{"from": "2026-01-05", "to": "2026-01-18"}
    )
    assert [(p["start"], p["page_views"]) for p in weekly["points"]] == [  # type: ignore[attr-defined]
        ("2026-01-05", 15),
        ("2026-01-12", 7),
    ]
    monthly = await report(
        client, admin_headers, group="month", **{"from": "2026-01-15", "to": "2026-02-10"}
    )
    # Точки подписаны началом месяца; считаются только дни внутри выбранного периода.
    assert [(p["start"], p["page_views"]) for p in monthly["points"]] == [  # type: ignore[attr-defined]
        ("2026-01-01", 0),
        ("2026-02-01", 20),
    ]


async def test_top_limit_is_applied(
    app: FastAPI,
    client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    catalog: dict[str, int],
) -> None:
    day = date(2026, 3, 2)
    await add_product_views(
        app,
        [
            (day, catalog["bosch_new"], 5),
            (day, catalog["denso_new"], 4),
            (day, catalog["bosch_pd"], 3),
        ],
    )
    body = await report(client, admin_headers, top=2, **{"from": "2026-03-01", "to": "2026-03-05"})
    assert [v["views"] for v in body["top_viewed"]] == [5, 4]  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------------ перенос просмотров
def flusher(app: FastAPI) -> StatsFlusher:
    resources = app.state.resources
    # В тестах счётчики лежат в обычной базе Redis приложения: перенос не зависит от того, какая это база.
    return StatsFlusher(resources.redis, resources.session_factory, app.state.settings)


async def page_views_in_db(app: FastAPI) -> dict[date, int]:
    async with app.state.resources.session_factory() as session:
        rows = (await session.execute(select(SiteStatsDaily))).scalars()
        return {r.day: r.page_views for r in rows}


async def test_flush_moves_counters_to_db_and_empties_redis(
    app: FastAPI, catalog: dict[str, int]
) -> None:
    redis = app.state.resources.redis
    await redis.set(f"{VIEWS_PREFIX}2026-03-02", 41)
    await redis.set(f"{VIEWS_PREFIX}2026-03-03", 5)
    await redis.hset(f"{PRODUCT_PREFIX}2026-03-02", mapping={str(catalog["bosch_new"]): 6})

    done = await flusher(app).run_once()

    assert done == {"pages": 46, "products": 6}
    assert await page_views_in_db(app) == {date(2026, 3, 2): 41, date(2026, 3, 3): 5}
    assert await redis.keys("stats:*") == []  # дошли до нуля: ключи убраны
    async with app.state.resources.session_factory() as session:
        product = (await session.execute(select(ProductViewsDaily))).scalar_one()
    assert (product.day, product.injector_id, product.views) == (
        date(2026, 3, 2),
        catalog["bosch_new"],
        6,
    )


async def test_repeated_flush_adds_to_existing_totals(
    app: FastAPI, catalog: dict[str, int]
) -> None:
    redis = app.state.resources.redis
    key = f"{VIEWS_PREFIX}2026-03-02"
    await redis.set(key, 10)
    await flusher(app).run_once()
    await redis.set(key, 3)  # за день пришло ещё
    await flusher(app).run_once()
    assert await page_views_in_db(app) == {date(2026, 3, 2): 13}

    # Без новых просмотров повторный проход ничего не меняет.
    assert await flusher(app).run_once() == {"pages": 0, "products": 0}
    assert await page_views_in_db(app) == {date(2026, 3, 2): 13}


async def test_views_added_during_flush_are_kept_for_the_next_one(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    redis = app.state.resources.redis
    key = f"{VIEWS_PREFIX}2026-03-02"
    await redis.set(key, 10)
    original = redis.eval

    async def eval_with_concurrent_view(*args: object, **kwargs: object) -> object:
        await redis.incr(key)  # сайт засчитал просмотр между чтением и вычитанием
        return await original(*args, **kwargs)

    monkeypatch.setattr(redis, "eval", eval_with_concurrent_view)
    assert (await flusher(app).run_once())["pages"] == 10
    monkeypatch.undo()

    assert int(await redis.get(key)) == 1  # осталось ровно то, что не успели перенести
    await flusher(app).run_once()
    assert await page_views_in_db(app) == {date(2026, 3, 2): 11}


async def test_flush_skips_unknown_products_and_foreign_keys(
    app: FastAPI, catalog: dict[str, int]
) -> None:
    redis = app.state.resources.redis
    day_key = f"{PRODUCT_PREFIX}2026-03-02"
    await redis.hset(
        day_key, mapping={str(catalog["bosch_new"]): 4, "999999": 9, "не-число": 3, "0": 0}
    )
    await redis.set("stats:views:мусор", 5)  # имя не похоже на дату
    await redis.set("stats:views:2026-03-02", "0")  # нулевой счётчик

    done = await flusher(app).run_once()

    assert done == {"pages": 0, "products": 4}  # неизвестный товар в итог не входит
    async with app.state.resources.session_factory() as session:
        rows = list((await session.execute(select(ProductViewsDaily))).scalars())
    assert [(r.injector_id, r.views) for r in rows] == [(catalog["bosch_new"], 4)]
    # Неизвестный товар вычтен, чтобы не повторяться на каждом проходе; посторонние ключи не тронуты.
    assert await redis.hget(day_key, "999999") is None
    assert await redis.get("stats:views:мусор") is not None


# ------------------------------------------------------------------------------------ срок хранения
async def test_retention_deletes_stats_older_than_a_year(
    app: FastAPI, infra_settings: Settings, catalog: dict[str, int]
) -> None:
    today = datetime.now(MSK).date()
    old, recent = today - timedelta(days=400), today - timedelta(days=10)
    await add_views(app, {old: 5, recent: 7})
    await add_product_views(
        app, [(old, catalog["bosch_new"], 1), (recent, catalog["bosch_new"], 2)]
    )

    cleaner = RetentionCleaner(app.state.resources.session_factory, infra_settings)
    deleted = await cleaner.run_once()

    assert deleted["site_stats_daily"] == 1
    assert deleted["product_views_daily"] == 1
    assert await page_views_in_db(app) == {recent: 7}
