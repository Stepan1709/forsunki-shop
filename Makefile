# Запускать из WSL (Linux-оболочка). Основные команды: make help.
SHELL := /bin/bash
COMPOSE := docker compose --env-file .env -f deploy/compose.yml -f deploy/compose.dev.yml
SERVICES := api web payment_stub
SECRETS := postgres_password redis_password rabbitmq_password jwt_secret s3_access_key s3_secret_key payment_api_key payment_webhook_secret grafana_admin_password

.DEFAULT_GOAL := help
.PHONY: help env secrets up up-full down logs migrate test e2e check-compose lint fmt create-admin seed bench chaos load tls prod-secrets prod-build prod-local prod-down prod-seed prod-e2e backup backup-verify

help: ## Список команд
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

env: ## Создать .env из .env.example, если его нет
	@test -f .env || { cp .env.example .env; echo "создан .env"; }

secrets: ## Сгенерировать dev-секреты в deploy/secrets/dev (существующие не трогает)
	@mkdir -p deploy/secrets/dev
	@for n in $(SECRETS); do \
	  f=deploy/secrets/dev/$$n; \
	  if [ ! -f $$f ]; then \
	    openssl rand -base64 33 | tr -d '/+=\n' > $$f; \
	    chmod 644 $$f; \
	    echo "создан $$f"; \
	  fi; \
	done
	# 644, а не 600: процессы в контейнерах (postgres, rabbitmq) работают не от владельца файла.
	# В prod права настраиваются на сервере, файлы лежат вне репозитория.

up: env secrets ## Поднять ядро (postgres, redis, rabbitmq + dev-инструменты)
	$(COMPOSE) up -d --build --wait

up-full: env secrets ## Ядро + логи, метрики и трассировка (профили logging, monitoring, tracing)
	OTEL_ENDPOINT=http://jaeger:4318 $(COMPOSE) --profile logging --profile monitoring --profile tracing up -d --build --wait

down: ## Остановить
	$(COMPOSE) --profile logging --profile monitoring --profile tracing down

logs: ## Логи всех контейнеров
	$(COMPOSE) logs -f --tail=100

migrate: env secrets ## Применить миграции (одноразовый контейнер migrate)
	$(COMPOSE) run --rm --build migrate

test: ## Unit + интеграционные тесты всех сервисов
	@for s in $(SERVICES); do \
	  echo "== $$s"; (cd services/$$s && uv run pytest) || exit 1; \
	done

e2e: ## Сквозные сценарии на запущенном стеке: заказ и оплата, путь через сайт, метрики и трасса
	cd services/api && uv run python ../../scripts/e2e_payment.py
	cd services/api && uv run python ../../scripts/e2e_web.py
	cd services/api && uv run python ../../scripts/e2e_observability.py

check-compose: ## Проверить, что у всех сервисов настроена ротация Docker-логов
	python3 scripts/check_compose.py

lint: ## ruff + mypy
	@for s in $(SERVICES); do \
	  echo "== $$s"; \
	  (cd services/$$s && uv run ruff check . && uv run ruff format --check . && uv run mypy app tests) || exit 1; \
	done

fmt: ## ruff format
	@for s in $(SERVICES); do (cd services/$$s && uv run ruff check --fix . && uv run ruff format .); done

create-admin: ## Создать администратора: make create-admin EMAIL=you@example.com
	@test -n "$(EMAIL)" || { echo "укажите EMAIL=..."; exit 1; }
	$(COMPOSE) run --rm migrate python -m app.cli create-admin --email $(EMAIL)

seed: env secrets ## Заполнить каталог демо-данными: make seed COUNT=5000 (пересоздаёт каталог)
	$(COMPOSE) run --rm migrate python -m app.cli seed --count $(or $(COUNT),5000) --reset

bench: ## Замер каталога до/после кэша (api должен быть запущен и каталог заполнен)
	cd services/api && uv run python ../../scripts/bench_catalog.py

chaos: ## Хаос-сценарии: убить воркер, остановить RabbitMQ, Redis, Postgres (стек должен быть запущен; make up)
	cd services/api && uv run python ../../scripts/chaos.py

load: ## Нагрузочные замеры Locust (каталог с кэшем и без, заказы при 1/2/4 воркерах); ~8 минут, стек должен быть запущен
	cd services/api && uv run python ../../scripts/load_matrix.py

# ---------------------------------------------------------------------------------------------- prod на этой машине
# Проверка prod-конфигурации локально: те же образы и тот же compose.prod.yml, что на сервере, но образы собираются здесь,
# сертификат самоподписанный (браузер предупредит), секреты отдельные от dev (deploy/secrets/prod).
GHCR_OWNER ?= stepan1709
PROD_TAG ?= local
PROD := SECRETS_DIR=./secrets/prod TAG=$(PROD_TAG) GHCR_OWNER=$(GHCR_OWNER) docker compose -p forsunki-prod --env-file .env -f deploy/compose.yml -f deploy/compose.prod.yml

tls: ## Самоподписанный сертификат для localhost в deploy/tls (для домена его заменит Let's Encrypt)
	@mkdir -p deploy/tls
	@test -f deploy/tls/privkey.pem || { openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
	  -keyout deploy/tls/privkey.pem -out deploy/tls/fullchain.pem -subj "/CN=localhost" \
	  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" 2>/dev/null && chmod 644 deploy/tls/*.pem && echo "создан deploy/tls"; }

prod-secrets: ## Секреты prod (deploy/secrets/prod) и пароль Kibana; существующие файлы не трогаются
	@mkdir -p deploy/secrets/prod
	@for n in $(SECRETS); do \
	  f=deploy/secrets/prod/$$n; \
	  if [ ! -f $$f ]; then openssl rand -base64 33 | tr -d '/+=\n' > $$f; chmod 644 $$f; echo "создан $$f"; fi; \
	done
	@if [ ! -f deploy/secrets/prod/kibana_htpasswd ]; then \
	  pw=$$(openssl rand -base64 18 | tr -d '/+=\n'); \
	  echo "kibana:$$(openssl passwd -apr1 $$pw)" > deploy/secrets/prod/kibana_htpasswd; chmod 644 deploy/secrets/prod/kibana_htpasswd; \
	  echo $$pw > deploy/secrets/prod/kibana_password; echo "Kibana: пользователь kibana, пароль в deploy/secrets/prod/kibana_password"; \
	fi

prod-build: ## Собрать образы под именами GHCR с тегом PROD_TAG (по умолчанию local)
	docker build -f services/api/Dockerfile -t ghcr.io/$(GHCR_OWNER)/forsunki-api:$(PROD_TAG) .
	docker build -f services/web/Dockerfile -t ghcr.io/$(GHCR_OWNER)/forsunki-web:$(PROD_TAG) .
	docker build -f services/payment_stub/Dockerfile -t ghcr.io/$(GHCR_OWNER)/forsunki-payment-stub:$(PROD_TAG) .

prod-local: env prod-secrets tls prod-build ## Поднять prod на этой машине: https://localhost (остановите dev-стек: make down)
	$(PROD) --profile mailsink up -d --wait

prod-seed: ## Заполнить каталог prod-стека демо-данными: make prod-seed COUNT=500
	$(PROD) run --rm migrate python -m app.cli seed --count $(or $(COUNT),500) --reset

prod-e2e: ## Сквозной сценарий через nginx и HTTPS на prod-стеке
	cd services/api && WEB_URL=https://localhost E2E_VERIFY_TLS=0 uv run python ../../scripts/e2e_web.py

prod-down: ## Остановить prod, поднятый командой prod-local
	$(PROD) --profile mailsink --profile logging --profile monitoring --profile tracing --profile backup down

backup: ## Разовая резервная копия Postgres в S3 (работает на dev-стеке)
	$(COMPOSE) --profile backup run --rm --build backup python3 /app/backup.py --once

backup-verify: ## Восстановить последнюю копию во временную БД и сверить данные
	$(COMPOSE) --profile backup run --rm --build backup python3 /app/backup.py --verify
