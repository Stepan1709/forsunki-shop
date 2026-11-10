#!/bin/sh
# Одноразовая настройка стека логов (идемпотентна: можно запускать повторно).
#   1. политика ILM (удаление индексов старше 7 дней) и шаблон индекса forsunki-logs-*;
#   2. data view, сохранённые поиски, визуализации и дашборд в Kibana.
set -eu

ES="${ELASTICSEARCH_URL:-http://elasticsearch:9200}"
KIBANA="${KIBANA_URL:-http://kibana:5601}"

put() { # put <url> <файл>
  curl -fsS -X PUT "$1" -H 'Content-Type: application/json' --data-binary "@$2" >/dev/null
}

echo "ILM и шаблон индекса"
put "$ES/_ilm/policy/forsunki-logs-retention" /config/elasticsearch/ilm-policy.json
put "$ES/_index_template/forsunki-logs" /config/elasticsearch/index-template.json

echo "Объекты Kibana"
curl -fsS -X POST "$KIBANA/api/saved_objects/_import?overwrite=true" \
  -H 'kbn-xsrf: true' --form file=@/config/kibana/saved-objects.ndjson >/dev/null

# Дашборд открывается по умолчанию.
curl -fsS -X POST "$KIBANA/api/kibana/settings" -H 'kbn-xsrf: true' -H 'Content-Type: application/json' \
  -d '{"changes":{"defaultRoute":"/app/dashboards#/view/forsunki-overview","timepicker:timeDefaults":"{\"from\":\"now-1h\",\"to\":\"now\"}","theme:darkMode":"system"}}' >/dev/null
echo "готово"
