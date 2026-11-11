# syntax=docker/dockerfile:1.7
# Образ api. Из него же запускаются worker и контейнер миграций (отличается только команда).
# Контекст сборки: корень репозитория.
#   docker build -f services/api/Dockerfile -t forsunki-api .

ARG PYTHON_VERSION=3.12.16

# --- Стадия сборки: зависимости ставим в venv, который потом копируем целиком ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Путь тот же, что в финальном образе: в venv прописаны абсолютные пути скриптов.
WORKDIR /srv/app
# Сначала только манифесты: слой с зависимостями переиспользуется, пока они не менялись.
COPY services/api/pyproject.toml services/api/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- Финальный образ: без uv, компиляторов и кэшей ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ENV PATH="/srv/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
# Не root: компрометация процесса не даёт прав на файловую систему контейнера.
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /srv/app
COPY --from=builder /srv/app/.venv ./.venv
COPY --chown=app:app services/api/app ./app
COPY --chown=app:app services/api/migrations ./migrations
COPY --chown=app:app services/api/alembic.ini ./alembic.ini
USER app
EXPOSE 8000
# Проверяем сам процесс (/health/live), а не зависимости: см. app/core/health.py.
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
# Exec-форма: SIGTERM приходит самому uvicorn, а не оболочке, иначе нет graceful shutdown.
# 25 секунд на завершение запросов при stop_grace_period 30s в compose.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "25", "--no-access-log"]
