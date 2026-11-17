"""Счётчики просмотров, графики и страница «Статистика» в админке."""

from datetime import date, timedelta

import httpx
import pytest
from fastapi import FastAPI
from redis.exceptions import RedisError

from app.charts import bar_chart, format_number, nice_ceiling
from app.routes.admin_stats import default_group, point_label, resolve_period
from app.stats import PRODUCT_PREFIX, VIEWS_PREFIX, is_bot, today_key
from tests.conftest import csrf_of
from tests.fake_api import FakeApi

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"}


async def views(app: FastAPI) -> int:
    raw = await app.state.redis.get(f"{VIEWS_PREFIX}{today_key()}")
    return int(raw) if raw else 0


async def product_views(app: FastAPI, injector_id: int) -> int:
    raw = await app.state.redis.hget(f"{PRODUCT_PREFIX}{today_key()}", str(injector_id))
    return int(raw) if raw else 0


# ------------------------------------------------------------------------------------ что считается
async def test_page_view_is_counted_for_a_browser(app: FastAPI, client: httpx.AsyncClient) -> None:
    await client.get("/", headers=BROWSER)
    await client.get("/shop", headers=BROWSER)
    await client.get("/repair", headers=BROWSER)
    assert await views(app) == 3


async def test_product_card_counts_page_and_product_view(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.get("/shop/1", headers=BROWSER)
    assert response.status_code == 200
    assert await views(app) == 1
    assert await product_views(app, 1) == 1
    await client.get("/shop/1", headers=BROWSER)
    assert await product_views(app, 1) == 2


async def test_logged_in_customer_is_counted(app: FastAPI, buyer: httpx.AsyncClient) -> None:
    await buyer.get("/shop/1", headers=BROWSER)
    assert await views(app) == 1
    assert await product_views(app, 1) == 1


@pytest.mark.parametrize(
    "headers",
    [
        {"User-Agent": "python-httpx/0.28"},  # скрипт
        {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1)"},  # поисковый робот
        {"User-Agent": ""},  # браузеры всегда присылают User-Agent
        {**BROWSER, "HX-Request": "true"},  # частичное обновление страницы
    ],
)
async def test_bots_and_htmx_updates_are_not_counted(
    app: FastAPI, client: httpx.AsyncClient, headers: dict[str, str]
) -> None:
    await client.get("/", headers=headers)
    await client.get("/shop/1", headers=headers)
    assert await views(app) == 0
    assert await product_views(app, 1) == 0


async def test_non_pages_errors_redirects_and_posts_are_not_counted(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    await client.get("/health/live", headers=BROWSER)
    await client.get("/metrics", headers=BROWSER)
    await client.get("/static/css/app.css", headers=BROWSER)
    await client.get("/нет-такой-страницы", headers=BROWSER)  # 404
    await client.get("/cart", headers=BROWSER)  # гостя перенаправляют на вход (303)
    await client.post("/login", headers=BROWSER, data={"email": "x"})
    assert await views(app) == 0


async def test_missing_product_is_not_counted_as_view(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    response = await client.get("/shop/999", headers=BROWSER)
    assert response.status_code == 404
    assert await product_views(app, 999) == 0
    assert await views(app) == 0


async def test_admin_browsing_is_not_counted(app: FastAPI, admin: httpx.AsyncClient) -> None:
    await admin.get("/shop/1", headers=BROWSER)
    await admin.get("/", headers=BROWSER)
    await admin.get("/admin/stats", headers=BROWSER)
    assert await views(app) == 0
    assert await product_views(app, 1) == 0


async def test_redis_failure_does_not_break_the_page(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: object, **kwargs: object) -> None:
        raise RedisError("недоступен")

    monkeypatch.setattr(app.state.redis, "incr", broken)
    monkeypatch.setattr(app.state.redis, "hincrby", broken)
    response = await client.get("/shop/1", headers=BROWSER)
    assert response.status_code == 200
    assert "Mercedes-Benz OM651" in response.text


def test_bot_detection() -> None:
    assert is_bot(None)
    assert is_bot("")
    assert is_bot("curl/8.0")
    assert is_bot("Mozilla/5.0 (compatible; bingbot/2.0)")
    assert not is_bot("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15 Safari/604.1")


# ------------------------------------------------------------------------------------ графики
def test_nice_ceiling_rounds_up_to_round_numbers() -> None:
    assert nice_ceiling(0) == 4
    assert nice_ceiling(7) == 8
    assert nice_ceiling(10) == 10
    assert nice_ceiling(130) == 150
    assert nice_ceiling(4200) == 5000
    assert format_number(1234567) == "1 234 567"


def test_bar_chart_draws_one_bar_per_point_with_tooltips() -> None:
    svg = bar_chart(["01.03", "02.03", "03.03"], [5, 0, 10], title="Просмотры", unit="просм.")
    assert svg.startswith("<svg")
    assert svg.count("<rect") == 3
    assert "01.03: 5 просм." in svg
    assert 'role="img"' in svg
    assert 'aria-label="Просмотры"' in svg


def test_bar_chart_escapes_labels_and_handles_empty_data() -> None:
    svg = bar_chart(["<script>alert(1)</script>"], [3], title='"><img src=x>')
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert "<img" not in svg
    empty = bar_chart([], [], title="Пусто")
    assert "<svg" in empty
    assert "<rect" not in empty
    with pytest.raises(ValueError, match="одной длины"):
        bar_chart(["a"], [1, 2], title="Ошибка")


def test_bar_chart_money_shows_rubles() -> None:
    svg = bar_chart(["01.03"], [1_500_000], title="Выручка", value_format="money")
    assert "15 000 ₽" in svg  # 1 500 000 копеек


def test_bar_chart_labels_x_axis_sparsely_for_long_periods() -> None:
    labels = [f"{d:02d}.01" for d in range(1, 29)] * 3
    svg = bar_chart(labels, [1] * len(labels), title="Много")
    assert svg.count("<rect") == len(labels)
    assert svg.count('text-anchor="middle"') <= 12  # подписи оси X не слипаются


# ------------------------------------------------------------------------------------ выбор периода
def test_presets_and_defaults_cover_whole_days_ending_today() -> None:
    start, end, selected, error = resolve_period("7", "", "")
    assert (end - start).days == 6
    assert (selected, error) == ("7", None)
    start, end, selected, _ = resolve_period("что-то", "", "")
    assert (end - start).days == 29
    assert selected == "30"


def test_custom_period_validation() -> None:
    assert resolve_period("custom", "2026-03-01", "2026-03-31")[:3] == (
        date(2026, 3, 1),
        date(2026, 3, 31),
        "custom",
    )
    for raw_from, raw_to in (
        ("", ""),
        ("2026-03-10", "2026-03-01"),
        ("2024-01-01", "2026-01-01"),
        ("x", "y"),
    ):
        *_, error = resolve_period("custom", raw_from, raw_to)
        assert error, (raw_from, raw_to)


def test_default_group_grows_with_period_length() -> None:
    start = date(2026, 1, 1)
    assert default_group(start, start + timedelta(days=29)) == "day"
    assert default_group(start, start + timedelta(days=89)) == "week"
    assert default_group(start, start + timedelta(days=364)) == "month"
    assert point_label(date(2026, 3, 5), "day", show_year=False) == "05.03"
    assert point_label(date(2026, 3, 1), "month", show_year=True) == "мар 26"


# ------------------------------------------------------------------------------------ страница админки
async def test_stats_page_shows_totals_charts_and_top_lists(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    page = await admin.get("/admin/stats?period=7")
    assert page.status_code == 200
    assert "Просмотры страниц" in page.text
    assert "<svg" in page.text
    assert "15 000,00 ₽" in page.text  # выручка за период
    assert "Чаще всего смотрят" in page.text
    assert "Чаще всего покупают" in page.text
    assert 'href="/admin/products/2"' in page.text

    sent = api.called("GET", "/admin/stats")[-1].params
    assert date.fromisoformat(sent["to"]) - date.fromisoformat(sent["from"]) == timedelta(days=6)
    assert sent["group"] == "day"


async def test_stats_page_escapes_product_titles_from_api(admin: httpx.AsyncClient) -> None:
    page = await admin.get("/admin/stats")
    assert "<b>Bosch</b>" not in page.text
    assert "&lt;b&gt;Bosch&lt;/b&gt;" in page.text


async def test_stats_page_custom_period_and_group_are_forwarded(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    await admin.get("/admin/stats?period=custom&from=2026-01-05&to=2026-03-30&group=week")
    sent = api.called("GET", "/admin/stats")[-1].params
    assert (sent["from"], sent["to"], sent["group"]) == ("2026-01-05", "2026-03-30", "week")

    await admin.get("/admin/stats?period=365")
    assert api.called("GET", "/admin/stats")[-1].params["group"] == "month"  # крупный шаг для года


async def test_stats_page_reports_bad_custom_period_and_falls_back(
    admin: httpx.AsyncClient, api: FakeApi
) -> None:
    page = await admin.get("/admin/stats?period=custom&from=2026-03-10&to=2026-03-01")
    assert page.status_code == 200
    assert "Начало периода позже его конца." in page.text
    sent = api.called("GET", "/admin/stats")[-1].params
    assert date.fromisoformat(sent["to"]) - date.fromisoformat(sent["from"]) == timedelta(days=29)


async def test_stats_page_is_closed_to_customers_and_menu_link_is_admin_only(
    client: httpx.AsyncClient,
) -> None:
    assert 'href="/admin/stats"' not in (await client.get("/")).text


async def test_customer_cannot_open_stats(buyer: httpx.AsyncClient, api: FakeApi) -> None:
    assert (await buyer.get("/admin/stats")).status_code == 403
    assert not api.called("GET", "/admin/stats")


async def test_admin_sees_stats_link_in_menu(admin: httpx.AsyncClient) -> None:
    page = await admin.get("/")
    assert 'href="/admin/stats"' in page.text
    assert csrf_of(page.text)
