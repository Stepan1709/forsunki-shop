# syntax=docker/dockerfile:1.7
# Образ сайта. Контекст сборки: корень репозитория.
#   docker build -f services/web/Dockerfile -t forsunki-web .

ARG PYTHON_VERSION=3.12.8

# --- Стадия css: Tailwind standalone CLI (без Node) собирает один минифицированный файл стилей ---
FROM debian:bookworm-slim AS css
ARG TAILWIND_VERSION=3.4.17
# Хэш зафиксирован: подменённый бинарник не пройдёт проверку и сборка остановится.
ARG TAILWIND_SHA256=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /usr/local/bin/tailwindcss \
      "https://github.com/tailwindlabs/tailwindcss/releases/download/v${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && echo "${TAILWIND_SHA256}  /usr/local/bin/tailwindcss" | sha256sum -c - \
    && chmod +x /usr/local/bin/tailwindcss
WORKDIR /build
COPY services/web/tailwind.config.js ./
COPY services/web/app/static/input.css ./app/static/input.css
# Tailwind смотрит в шаблоны и оставляет в CSS только использованные классы.
COPY services/web/app/templates ./app/templates
RUN tailwindcss -c tailwind.config.js -i app/static/input.css -o /build/app.css --minify

# --- Стадия сборки зависимостей Python ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /srv/app
COPY services/web/pyproject.toml services/web/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- Финальный образ ---
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ENV PATH="/srv/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app
WORKDIR /srv/app
COPY --from=builder /srv/app/.venv ./.venv
COPY --chown=app:app services/web/app ./app
COPY --from=css --chown=app:app /build/app.css ./app/static/css/app.css
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "25", "--no-access-log"]
