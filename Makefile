# Запускать из WSL (Linux-оболочка). Основные команды: make help.
SHELL := /bin/bash
COMPOSE := docker compose --env-file .env -f deploy/compose.yml -f deploy/compose.dev.yml
SERVICES := api web payment_stub
SECRETS := postgres_password redis_password rabbitmq_password jwt_secret s3_access_key s3_secret_key payment_api_key payment_webhook_secret

.DEFAULT_GOAL := help
.PHONY: help env secrets up up-full down logs migrate test e2e check-compose lint fmt create-admin seed bench

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

up-full: up ## Ядро + логирование и метрики (профили logging и monitoring)
	$(COMPOSE) --profile logging --profile monitoring up -d --wait

down: ## Остановить
	$(COMPOSE) --profile logging --profile monitoring down

logs: ## Логи всех контейнеров
	$(COMPOSE) logs -f --tail=100

migrate: env secrets ## Применить миграции (одноразовый контейнер migrate)
	$(COMPOSE) run --rm --build migrate

test: ## Unit + интеграционные тесты всех сервисов
	@for s in $(SERVICES); do \
	  echo "== $$s"; (cd services/$$s && uv run pytest) || exit 1; \
	done

e2e: ## Сквозные сценарии на запущенном стеке: заказ и оплата через api, путь покупателя через сайт
	cd services/api && uv run python ../../scripts/e2e_payment.py
	cd services/api && uv run python ../../scripts/e2e_web.py

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
