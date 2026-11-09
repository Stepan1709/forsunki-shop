# ForsunkiShop

Интернет-магазин дизельных форсунок со страницей ремонта. Проект-витрина асинхронного бэкенда на Python:
FastAPI, PostgreSQL, Redis, RabbitMQ, идемпотентная обработка заказов и платежей, наблюдаемость
(логи, метрики, трассировка) и воспроизводимая инфраструктура на Docker Compose.

Правила проекта: [CLAUDE.md](CLAUDE.md). План работ и статус этапов: [docs/PLAN.md](docs/PLAN.md).
Архитектурные решения: [docs/adr/](docs/adr/).

## Архитектура

```mermaid
flowchart LR
    user([Покупатель]) --> nginx[nginx]
    nginx --> web["web<br/>FastAPI + Jinja2 + HTMX"]
    web -->|HTTP, httpx| api["api<br/>FastAPI, модульный монолит"]
    api --> pg[(PostgreSQL)]
    api --> redis[(Redis)]
    api -->|outbox| mq{{RabbitMQ}}
    mq --> worker["worker<br/>платежи, письма, истечение заказов"]
    worker --> pg
    worker -->|PaymentGateway| stub["payment_stub"]
    stub -->|webhook, HMAC| api
    web --> redis
    worker --> mail[Mailpit / SMTP]
```

Схема показывает целевое состояние, она уточняется по мере выполнения этапов.

## Быстрый старт (WSL, Docker Desktop с WSL-интеграцией)

Нужны: Docker, `make`, `uv`, `git`.

```bash
make secrets   # случайные пароли в deploy/secrets/dev (не коммитятся)
make up        # postgres, redis, rabbitmq + Adminer, Mailpit
make down
```

| Сервис | Адрес |
|---|---|
| RabbitMQ UI | http://localhost:15672 (пользователь `forsunki`, пароль в `deploy/secrets/dev/rabbitmq_password`) |
| API (Swagger) | http://localhost:8001/docs |
| PostgreSQL | localhost:5433 (порт меняется через `POSTGRES_PUBLISH_PORT` в `.env`) |
| Adminer | http://localhost:8080 |
| Mailpit | http://localhost:8025 |

Остальные команды: `make help`. Репозиторий лежит на диске `D:`, команды запускаются из WSL
(`/mnt/d/Opencode_proj/ForsunkiShop`).

Для Elasticsearch (этап 9) в WSL понадобится `sudo sysctl -w vm.max_map_count=262144`.

## Статус

Этапы 0 (каркас) и 1 (инфраструктурное ядро `api`) готовы. Следующий: этап 2, пользователи и авторизация.

## Этап 1: что проверено

| Критерий | Результат |
|---|---|
| `docker stop api` во время запроса на 6 с | запрос завершился `200`, остановка 5 с, код выхода 143 |
| `/health/ready` при остановленном Postgres | `503`, `checks.db = fail`; `/health/live` остаётся `200`; после возврата БД снова `200` |
| Логи | JSON в stdout с `timestamp`, `level`, `service`, `env`, `event`, `correlation_id` |
| Ошибки | единый формат Problem Details (`application/problem+json`) |
| Миграции | отдельный контейнер `migrate` (`make migrate`), `upgrade` и `downgrade` проверены тестом |

Тесты ядра идут на настоящих Postgres, Redis и RabbitMQ (Testcontainers), `make test`. Подробности в [docs/PLAN.md](docs/PLAN.md).
