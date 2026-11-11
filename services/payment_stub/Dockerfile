# syntax=docker/dockerfile:1.7
# Образ payment_stub. Контекст сборки: корень репозитория.
#   docker build -f services/payment_stub/Dockerfile -t forsunki-payment-stub .

ARG PYTHON_VERSION=3.12.16

FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Тот же путь, что в runtime-стадии: в venv зашиты абсолютные пути скриптов.
WORKDIR /srv/app
COPY services/payment_stub/pyproject.toml services/payment_stub/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ENV PATH="/srv/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 app && useradd --system --uid 10001 --gid app --no-create-home app \
    && mkdir /data && chown app:app /data
WORKDIR /srv/app
COPY --from=builder /srv/app/.venv ./.venv
# Шаблоны и статика лежат внутри app/, поэтому копируются вместе с кодом.
COPY --chown=app:app services/payment_stub/app ./app
USER app
VOLUME /data
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"]
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "25", "--no-access-log"]
