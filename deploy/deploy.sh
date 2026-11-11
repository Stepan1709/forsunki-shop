#!/usr/bin/env bash
# Выкатка и откат prod на сервере (запускается из каталога deploy/).
#
#   ./deploy.sh up <тег>      скачать образы тега, перезапустить сервисы, дождаться здоровья
#   ./deploy.sh rollback      вернуться на предыдущий тег (одной командой)
#   ./deploy.sh status        какой тег сейчас выкачен и какой был до него
#
# Если после выкатки сервисы не стали здоровыми, скрипт сам откатывается на предыдущий тег. Теги хранятся в файле .deploy-state
# (текущий и предыдущий). Миграции идут одноразовым контейнером перед запуском api: откат кода НЕ откатывает схему БД, поэтому
# миграции пишутся обратно совместимыми (старый код работает на новой схеме), см. docs/adr.
#
# Переменные окружения: SECRETS_DIR (каталог секретов), GHCR_OWNER, SITE_URL; COMPOSE_EXTRA_ARGS (например, --profile logging);
# SKIP_PULL=1 не скачивать образы (локальная проверка, образы уже собраны на этой машине).
set -euo pipefail

cd "$(dirname "$0")"
STATE=".deploy-state"
COMPOSE=(docker compose -f compose.yml -f compose.prod.yml)
read -r -a EXTRA <<<"${COMPOSE_EXTRA_ARGS:-}"
TIMEOUT="${DEPLOY_TIMEOUT_SECONDS:-240}"

current_tag() { [ -f "$STATE" ] && sed -n 1p "$STATE" || true; }
previous_tag() { [ -f "$STATE" ] && sed -n 2p "$STATE" || true; }

apply() {
  local tag="$1"
  export TAG="$tag"
  echo "==> выкатываем тег $tag"
  if [ -z "${SKIP_PULL:-}" ]; then
    "${COMPOSE[@]}" ${EXTRA[@]+"${EXTRA[@]}"} pull --quiet
  fi
  "${COMPOSE[@]}" ${EXTRA[@]+"${EXTRA[@]}"} up -d --remove-orphans --wait --wait-timeout "$TIMEOUT"
}

case "${1:-}" in
  up)
    tag="${2:?укажите тег: ./deploy.sh up v0.1.0}"
    old="$(current_tag)"
    if apply "$tag"; then
      printf '%s\n%s\n' "$tag" "$old" >"$STATE"
      echo "==> готово: $tag (предыдущий: ${old:-нет})"
    else
      echo "!! выкатка $tag не удалась" >&2
      if [ -n "$old" ]; then
        echo "==> автоматический откат на $old" >&2
        apply "$old" || true
      fi
      exit 1
    fi
    ;;
  rollback)
    prev="$(previous_tag)"
    [ -n "$prev" ] || { echo "нет предыдущего тега для отката" >&2; exit 1; }
    cur="$(current_tag)"
    apply "$prev"
    printf '%s\n%s\n' "$prev" "$cur" >"$STATE"
    echo "==> откат выполнен: $cur -> $prev"
    ;;
  status)
    echo "текущий:    $(current_tag)"
    echo "предыдущий: $(previous_tag)"
    ;;
  *)
    echo "использование: $0 up <тег> | rollback | status" >&2
    exit 2
    ;;
esac
