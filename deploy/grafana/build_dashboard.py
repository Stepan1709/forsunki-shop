"""Собирает deploy/grafana/dashboards/forsunki.json: дашборд «Forsunki: обзор системы».

Запуск: python3 deploy/grafana/build_dashboard.py
Зачем скрипт: JSON дашборда Grafana длинный и однообразный, а панели здесь описаны одной строкой запроса. Чтобы изменить дашборд,
правьте этот файл, запустите его и закоммитьте получившийся JSON. Grafana подхватывает файл при старте (provisioning).
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "prometheus"}
panels: list[dict[str, object]] = []
_next_id = 0


def _target(expr: str, legend: str) -> dict[str, object]:
    return {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": chr(65 + len(panels) % 26),
    }


def _add(panel: dict[str, object]) -> None:
    global _next_id
    _next_id += 1
    panel["id"] = _next_id
    panel["datasource"] = DS
    panels.append(panel)


def stat(
    title: str,
    expr: str,
    x: int,
    *,
    unit: str = "short",
    red_above: float | None = None,
    green_above: float | None = None,
    decimals: int | None = None,
) -> None:
    steps: list[dict[str, object]] = [{"color": "green", "value": None}]
    if red_above is not None:
        steps.append({"color": "red", "value": red_above})
    if green_above is not None:
        steps = [
            {"color": "red", "value": None},
            {"color": "orange", "value": green_above / 2},
            {"color": "green", "value": green_above},
        ]
    defaults: dict[str, object] = {"unit": unit, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None:
        defaults["decimals"] = decimals
    _add(
        {
            "type": "stat",
            "title": title,
            "gridPos": {"x": x, "y": 0, "w": 4, "h": 4},
            "targets": [{"datasource": DS, "expr": expr, "refId": "A", "instant": True}],
            "fieldConfig": {"defaults": defaults, "overrides": []},
            "options": {
                "colorMode": "background",
                "graphMode": "none",
                "reduceOptions": {"calcs": ["lastNotNull"]},
            },
        }
    )


def series(
    title: str,
    y: int,
    x: int,
    w: int,
    queries: list[tuple[str, str]],
    *,
    unit: str = "short",
    stack: bool = False,
    h: int = 8,
) -> None:
    custom: dict[str, object] = {"lineWidth": 2, "fillOpacity": 15, "showPoints": "never"}
    if stack:
        custom["stacking"] = {"mode": "normal", "group": "A"}
    _add(
        {
            "type": "timeseries",
            "title": title,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [
                {"datasource": DS, "expr": e, "legendFormat": legend, "refId": chr(65 + i)}
                for i, (e, legend) in enumerate(queries)
            ],
            "fieldConfig": {"defaults": {"unit": unit, "custom": custom}, "overrides": []},
            "options": {
                "legend": {"displayMode": "list", "placement": "bottom"},
                "tooltip": {"mode": "multi"},
            },
        }
    )


def bars(title: str, y: int, x: int, w: int, expr: str, legend: str, h: int = 8) -> None:
    _add(
        {
            "type": "bargauge",
            "title": title,
            "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [
                {
                    "datasource": DS,
                    "expr": expr,
                    "legendFormat": legend,
                    "refId": "A",
                    "instant": True,
                }
            ],
            "fieldConfig": {"defaults": {"unit": "short", "min": 0}, "overrides": []},
            "options": {
                "orientation": "horizontal",
                "displayMode": "gradient",
                "reduceOptions": {"calcs": ["lastNotNull"]},
            },
        }
    )


DLQ = 'queue=~".*[.]dlq"'
NOT_DLQ = 'queue!~".*[.]dlq"'

# --- верхняя строка: общее состояние ---
stat("Сервисы в сети", 'sum(up{job=~"api|web|worker"})', 0, green_above=3, decimals=0)
stat(
    "Сообщений в DLQ",
    f"sum(rabbitmq_detailed_queue_messages_ready{{{DLQ}}}) or vector(0)",
    4,
    red_above=1,
    decimals=0,
)
stat(
    "Ошибки 5xx, запр/с",
    'sum(rate(http_requests_total{status=~"5.."}[1m])) or vector(0)',
    8,
    red_above=0.05,
    decimals=2,
)
stat(
    "Попаданий в кэш (5 мин)",
    'sum(rate(cache_requests_total{result="hit"}[5m])) / sum(rate(cache_requests_total{result=~"hit|miss"}[5m]))',
    12,
    unit="percentunit",
    decimals=0,
)
stat("Просрочка outbox, с", "max(outbox_oldest_overdue_seconds)", 16, red_above=60, decimals=1)
stat("Событий ждут подписчика", "max(outbox_unroutable_events)", 20, decimals=0)

# --- HTTP ---
series(
    "Запросы в секунду",
    4,
    0,
    12,
    [
        ("sum by (job) (rate(http_requests_total[1m]))", "{{job}}"),
    ],
    unit="reqps",
)
series(
    "Ответы с ошибкой 5xx, по маршрутам",
    4,
    12,
    12,
    [
        ('sum by (job, route) (rate(http_requests_total{status=~"5.."}[1m]))', "{{job}} {{route}}"),
    ],
    unit="reqps",
)
series(
    "Время ответа (p50, p95, p99)",
    12,
    0,
    12,
    [
        (
            'histogram_quantile(0.5, sum by (le) (rate(http_request_duration_seconds_bucket{job="api"}[1m])))',
            "api p50",
        ),
        (
            'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="api"}[1m])))',
            "api p95",
        ),
        (
            'histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{job="api"}[1m])))',
            "api p99",
        ),
        (
            'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="web"}[1m])))',
            "web p95",
        ),
    ],
    unit="s",
)
series(
    "Медленнейшие маршруты api (p95)",
    12,
    12,
    12,
    [
        (
            'histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket{job="api"}[2m])))',
            "{{route}}",
        ),
    ],
    unit="s",
)

# --- очереди ---
series(
    "Сообщений в очередях (готовы к выдаче)",
    20,
    0,
    12,
    [
        (f"sum by (queue) (rabbitmq_detailed_queue_messages_ready{{{NOT_DLQ}}})", "{{queue}}"),
    ],
    stack=True,
)
series(
    "Сообщений в DLQ",
    20,
    12,
    12,
    [
        (f"sum by (queue) (rabbitmq_detailed_queue_messages_ready{{{DLQ}}})", "{{queue}}"),
    ],
)
series(
    "Обработка сообщений, шт/с по исходам",
    28,
    0,
    12,
    [
        ("sum by (queue, outcome) (rate(consumer_messages_total[1m]))", "{{queue}} {{outcome}}"),
    ],
)
series(
    "Время обработки сообщения (p95)",
    28,
    12,
    12,
    [
        (
            "histogram_quantile(0.95, sum by (le, queue) (rate(consumer_message_duration_seconds_bucket[2m])))",
            "{{queue}}",
        ),
    ],
    unit="s",
)

# --- бизнес ---
bars("Заказы по статусам", 36, 0, 8, "max by (status) (orders_by_status)", "{{status}}")
series(
    "Попадания в кэш каталога",
    36,
    8,
    8,
    [
        (
            'sum(rate(cache_requests_total{result="hit"}[1m])) / sum(rate(cache_requests_total{result=~"hit|miss"}[1m]))',
            "доля попаданий",
        ),
    ],
    unit="percentunit",
)
series(
    "Outbox: ожидают отправки",
    36,
    16,
    8,
    [
        ("max(outbox_pending_events)", "всего неотправленных"),
        ("max(outbox_unroutable_events)", "ждут подписчика"),
    ],
)

dashboard = {
    "uid": "forsunki-overview",
    "title": "Forsunki: обзор системы",
    "tags": ["forsunki"],
    "timezone": "browser",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "10s",
    "time": {"from": "now-30m", "to": "now"},
    "panels": panels,
}
out = Path(__file__).parent / "dashboards" / "forsunki.json"
out.write_text(json.dumps(dashboard, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"записано {len(panels)} панелей в {out.name}")
