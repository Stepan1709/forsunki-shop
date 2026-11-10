"""Собирает saved-objects.ndjson: data view, сохранённые поиски, визуализации и дашборд Kibana.

Запуск: python3 deploy/kibana/build_saved_objects.py
Зачем скрипт: объекты Kibana хранят настройки панелей вложенными JSON-строками, вручную их легко сломать.
Файл ndjson лежит в репозитории, setup.sh импортирует его в Kibana при каждом `make up-full`.
"""

import json
from pathlib import Path

INDEX_ID = "forsunki-logs"
INDEX_REF = {
    "name": "kibana_index",
    "type": "index-pattern",
    "id": INDEX_ID,
}
SEARCH_REF_NAME = "kibanaSavedObjectMeta.searchSourceJSON.index"


def search_source(query: str) -> str:
    return json.dumps(
        {
            "query": {"query": query, "language": "kuery"},
            "filter": [],
            "indexRefName": SEARCH_REF_NAME,
        }
    )


def ref() -> list[dict[str, str]]:
    return [{"name": SEARCH_REF_NAME, "type": "index-pattern", "id": INDEX_ID}]


def saved_search(
    object_id: str, title: str, query: str, columns: list[str], sort: list[list[str]]
) -> dict[str, object]:
    return {
        "id": object_id,
        "type": "search",
        "attributes": {
            "title": title,
            "description": "",
            "columns": columns,
            "sort": sort,
            "kibanaSavedObjectMeta": {"searchSourceJSON": search_source(query)},
        },
        "references": ref(),
    }


def bar_params(value_title: str, *, stacked: bool = True) -> dict[str, object]:
    return {
        "type": "histogram",
        "grid": {"categoryLines": False},
        "categoryAxes": [
            {
                "id": "CategoryAxis-1",
                "type": "category",
                "position": "bottom",
                "show": True,
                "style": {},
                "scale": {"type": "linear"},
                "labels": {"show": True, "filter": True, "truncate": 100},
                "title": {},
            }
        ],
        "valueAxes": [
            {
                "id": "ValueAxis-1",
                "name": "LeftAxis-1",
                "type": "value",
                "position": "left",
                "show": True,
                "style": {},
                "scale": {"type": "linear", "mode": "normal"},
                "labels": {"show": True, "rotate": 0, "filter": False, "truncate": 100},
                "title": {"text": value_title},
            }
        ],
        "seriesParams": [
            {
                "show": True,
                "type": "histogram",
                "mode": "stacked" if stacked else "normal",
                "data": {"label": value_title, "id": "1"},
                "valueAxis": "ValueAxis-1",
                "drawLinesBetweenPoints": True,
                "lineWidth": 2,
                "showCircles": True,
            }
        ],
        "addTooltip": True,
        "addLegend": True,
        "legendPosition": "right",
        "times": [],
        "addTimeMarker": False,
        "labels": {"show": False},
        "thresholdLine": {
            "show": False,
            "value": 10,
            "width": 1,
            "style": "full",
            "color": "#E7664C",
        },
    }


def visualization(
    object_id: str,
    title: str,
    vis_type: str,
    aggs: list[dict[str, object]],
    params: dict[str, object],
    query: str,
) -> dict[str, object]:
    vis_state = {"title": title, "type": vis_type, "aggs": aggs, "params": params}
    return {
        "id": object_id,
        "type": "visualization",
        "attributes": {
            "title": title,
            "description": "",
            "visState": json.dumps(vis_state),
            "uiStateJSON": "{}",
            "kibanaSavedObjectMeta": {"searchSourceJSON": search_source(query)},
        },
        "references": ref(),
    }


COUNT = {"id": "1", "enabled": True, "type": "count", "params": {}, "schema": "metric"}


def terms(agg_id: str, field: str, schema: str, size: int = 10) -> dict[str, object]:
    return {
        "id": agg_id,
        "enabled": True,
        "type": "terms",
        "params": {
            "field": field,
            "orderBy": "1",
            "order": "desc",
            "size": size,
            "otherBucket": False,
            "missingBucket": False,
        },
        "schema": schema,
    }


def date_histogram(agg_id: str) -> dict[str, object]:
    return {
        "id": agg_id,
        "enabled": True,
        "type": "date_histogram",
        "params": {
            "field": "@timestamp",
            "useNormalizedEsInterval": True,
            "scaleMetricValues": False,
            "interval": "auto",
            "drop_partials": False,
            "min_doc_count": 1,
            "extended_bounds": {},
        },
        "schema": "segment",
    }


ERRORS = "level:error or level:critical"

objects: list[dict[str, object]] = [
    {
        "id": INDEX_ID,
        "type": "index-pattern",
        "attributes": {"title": "forsunki-logs-*", "timeFieldName": "@timestamp"},
        "references": [],
    },
    saved_search(
        "forsunki-search-slow",
        "Медленные запросы (дольше 500 мс)",
        "duration_ms > 500",
        ["container_service", "method", "path", "status", "duration_ms", "correlation_id"],
        [["duration_ms", "desc"]],
    ),
    saved_search(
        "forsunki-search-errors",
        "Последние ошибки",
        ERRORS,
        ["container_service", "level", "event", "message", "correlation_id"],
        [["@timestamp", "desc"]],
    ),
    saved_search(
        "forsunki-search-trace",
        "Цепочка запроса (введите correlation_id в строку поиска)",
        "",
        ["container_service", "level", "event", "method", "path", "status", "correlation_id"],
        [["@timestamp", "asc"]],
    ),
    visualization(
        "forsunki-vis-errors-count",
        "Ошибок за период",
        "metric",
        [COUNT],
        {
            "addTooltip": True,
            "addLegend": False,
            "type": "metric",
            "metric": {
                "percentageMode": False,
                "useRanges": False,
                "colorSchema": "Green to Red",
                "metricColorMode": "None",
                "colorsRange": [{"from": 0, "to": 10000}],
                "labels": {"show": True},
                "invertColors": False,
                "style": {
                    "bgFill": "#000",
                    "bgColor": False,
                    "labelColor": False,
                    "subText": "",
                    "fontSize": 48,
                },
            },
        },
        ERRORS,
    ),
    visualization(
        "forsunki-vis-errors-by-service",
        "Ошибки по сервисам",
        "histogram",
        [COUNT, terms("2", "container_service", "segment")],
        bar_params("Ошибок"),
        ERRORS,
    ),
    visualization(
        "forsunki-vis-levels",
        "Записи по уровням во времени",
        "histogram",
        [COUNT, date_histogram("2"), terms("3", "level", "group", 6)],
        bar_params("Записей"),
        "",
    ),
    visualization(
        "forsunki-vis-avg-duration",
        "Среднее время ответа по сервисам (мс)",
        "histogram",
        [
            {
                "id": "1",
                "enabled": True,
                "type": "avg",
                "params": {"field": "duration_ms"},
                "schema": "metric",
            },
            terms("2", "container_service", "segment"),
        ],
        {**bar_params("Среднее, мс", stacked=False), "addLegend": False},
        "duration_ms:*",
    ),
]

PANELS = [
    # (id объекта, тип, x, y, ширина, высота)
    ("forsunki-vis-errors-count", "visualization", 0, 0, 12, 8),
    ("forsunki-vis-errors-by-service", "visualization", 12, 0, 18, 8),
    ("forsunki-vis-avg-duration", "visualization", 30, 0, 18, 8),
    ("forsunki-vis-levels", "visualization", 0, 8, 48, 10),
    ("forsunki-search-errors", "search", 0, 18, 48, 12),
    ("forsunki-search-slow", "search", 0, 30, 48, 12),
    ("forsunki-search-trace", "search", 0, 42, 48, 14),
]

panels_json = []
references = []
for i, (object_id, object_type, x, y, w, h) in enumerate(PANELS, start=1):
    panels_json.append(
        {
            "version": "8.17.0",
            "type": object_type,
            "gridData": {"x": x, "y": y, "w": w, "h": h, "i": str(i)},
            "panelIndex": str(i),
            "embeddableConfig": {},
            "panelRefName": f"panel_{i}",
        }
    )
    references.append({"name": f"panel_{i}", "type": object_type, "id": object_id})

objects.append(
    {
        "id": "forsunki-overview",
        "type": "dashboard",
        "attributes": {
            "title": "Forsunki: обзор логов",
            "description": "Ошибки по сервисам, медленные запросы и поиск цепочки по correlation_id.",
            "panelsJSON": json.dumps(panels_json),
            "optionsJSON": json.dumps(
                {"useMargins": True, "syncColors": False, "hidePanelTitles": False}
            ),
            "timeRestore": False,
            "kibanaSavedObjectMeta": {
                "searchSourceJSON": json.dumps(
                    {"query": {"query": "", "language": "kuery"}, "filter": []}
                )
            },
        },
        "references": references,
    }
)

out = Path(__file__).with_name("saved-objects.ndjson")
out.write_text(
    "\n".join(json.dumps(o, ensure_ascii=False) for o in objects) + "\n", encoding="utf-8"
)
print(f"записано {len(objects)} объектов в {out.name}")
