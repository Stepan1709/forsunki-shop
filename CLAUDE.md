# CLAUDE.md — правила проекта «ForsunkiShop»

ForsunkiShop: интернет-магазин дизельных форсунок со страницей ремонта. Проект для портфолио бэкенд-разработчика на Python. Главная цель: показать промышленный асинхронный бэкенд (FastAPI, PostgreSQL, Redis, RabbitMQ), надёжную обработку заказов и платежей (идемпотентность, статусы, повторы), наблюдаемость (логи → Elasticsearch/S3 → Kibana) и воспроизводимую инфраструктуру (Docker Compose, CI/CD).

Работаем по этапам из `docs/PLAN.md`. Один этап = одна ветка = один PR. К следующему этапу переходим, когда выполнены критерии готовности текущего.

## 1. Что делает магазин

Страницы сайта:
- **Главная:** что это за сайт и что он предоставляет, переходы в магазин и на страницу ремонта.
- **Магазин:** список форсунок (фильтры по производителю, типу, состоянию; поиск по артикулу и применимости), карточка товара.
- **Ремонт:** описание услуги, примерные цены, контакты, форма заявки на ремонт.
- **Корзина** и оформление заказа, страница статуса оплаты.
- **Профиль:** данные пользователя, история заказов и их статусы.
- Вход и регистрация.

Товар: артикул (OEM), производитель (Bosch, Denso, Delphi, Siemens и т.д.), тип (Common Rail, насос-форсунка), состояние (новая / восстановленная), цена, остаток, применимость к двигателям.

## 2. Архитектура

Модульный монолит (бэкенд) + отдельные процессы вокруг него. Микросервисы на домены (users/catalog/orders/...) **не делаем**: для этого проекта они добавили бы сложность без пользы.

| Компонент | Роль |
|---|---|
| `api` | Бэкенд: FastAPI, вся бизнес-логика, REST API. Модули: users, catalog, cart, orders, payments, repair |
| `worker` | Тот же код и образ, что `api`, другая команда запуска. Consumer'ы RabbitMQ: инициация платежа, письма, истечение неоплаченных заказов, заявки на ремонт, outbox relay |
| `web` | Сайт: FastAPI + Jinja2 + HTMX (+ Alpine.js для мелочей), ходит в `api` по HTTP через httpx. Работает как BFF: токены хранятся на сервере в Redis, в cookie только id сессии |
| `payment_stub` | Заглушка платёжного провайдера: создаёт платёж, показывает страницу «Оплатить / Отклонить», шлёт подписанный webhook. Заменяется реальной онлайн-кассой |
| PostgreSQL | Одна БД: пользователи, товары, корзины, заказы, платежи, outbox, inbox |
| Redis | Кэш каталога, сессии `web`, rate limiting, быстрые проверки идемпотентности |
| RabbitMQ | Сообщения и задачи воркерам |
| Fluent Bit → Elasticsearch + S3 → Kibana | Сбор, хранение и просмотр логов |
| Prometheus + Grafana | Метрики |
| nginx | Единственная точка входа в prod (TLS) |

Платёжный провайдер подключается через интерфейс `PaymentGateway` (Protocol). Реализации: `StubPaymentGateway` (сейчас), реальная касса (потом). Бизнес-код знает только интерфейс.

### Сценарий заказа

```
web ─► api: POST /orders (Idempotency-Key)
   транзакция: order(PENDING_PAYMENT) + резерв остатков + outbox(order.created)
 relay ─► RabbitMQ: order.created
 worker: создаёт платёж через PaymentGateway ─► payment_stub, статус платежа в БД
 покупатель платит на странице заглушки
 payment_stub ─► api: POST /payments/webhook (подпись HMAC)
   условный UPDATE: PENDING_PAYMENT → PAID (только один раз) + outbox(order.paid)
 worker: письмо покупателю, прочие реакции
 Неоплаченный заказ: отложенное сообщение order.expire (TTL + DLX) → EXPIRED, остатки возвращаются
```

Статусы заказа: `PENDING_PAYMENT → PAID | PAYMENT_FAILED | CANCELLED | EXPIRED`. Переходы только по таблице допустимых переходов; недопустимый переход = ошибка, не молчаливая перезапись.

## 3. Стек

- Python 3.12, FastAPI, Pydantic v2, pydantic-settings
- SQLAlchemy 2.0 async (asyncpg) + Alembic
- redis-py (`redis.asyncio`)
- aio-pika (только асинхронный клиент)
- httpx (`AsyncClient`) для вызовов между процессами
- structlog (JSON-логи), prometheus-client
- argon2-cffi (пароли), PyJWT
- aioboto3 или aiobotocore для S3 (если приложение работает с S3, например картинки товаров)
- Jinja2 + HTMX + Alpine.js, Tailwind через standalone CLI (без Node). Все статические библиотеки лежат в репозитории или собираются в образ, не подключаются с CDN
- Админка: SQLAdmin (Python), доступ по роли admin
- Тесты: pytest, pytest-asyncio, testcontainers, httpx (ASGITransport), Locust для нагрузки
- Качество: Ruff (lint + format), mypy, pre-commit, gitleaks
- Менеджер пакетов: uv (зафиксировать на этапе 0)
- Контейнеры: Docker, Compose v2. CI/CD: GitHub Actions, образы в GHCR

Всё на Python, где это возможно. Не-Python части только инфраструктурные (Postgres, Redis, RabbitMQ, nginx, Fluent Bit, ELK, Prometheus, Grafana).

## 4. Структура репозитория

```
services/
  api/
    app/
      core/          # config, lifespan-ресурсы, logging, security, errors, messaging, outbox/inbox
      modules/       # users, catalog, cart, orders, payments, repair
        <module>/    # router.py, service.py, repository.py, schemas.py, models.py
      worker/        # consumers, relay, точка входа воркера
      main.py
    migrations/  tests/  Dockerfile  pyproject.toml
  web/
    app/ (routes, templates, static, api_client)  tests/  Dockerfile  pyproject.toml
  payment_stub/
    app/  tests/  Dockerfile  pyproject.toml
deploy/
  compose.yml  compose.dev.yml  compose.prod.yml
  nginx/  fluent-bit/  elasticsearch/  kibana/  prometheus/  grafana/
  secrets/            # только *.example, реальные значения не коммитить
docs/PLAN.md  docs/adr/
.github/workflows/    # ci.yml, release.yml
Makefile  README.md  CLAUDE.md
```

Внутри `api` слои: router → service → repository. Бизнес-логика в service, SQL только в repository. Роутеры тонкие.

## 5. Жизненный цикл ресурсов (lifespan) и graceful shutdown

- Все клиенты и пулы создаются **заранее, один раз**, в `lifespan` приложения и хранятся в `app.state`: `AsyncEngine` + `async_sessionmaker`, `redis.asyncio.ConnectionPool`/клиент, `aio_pika.connect_robust` соединение и каналы, `httpx.AsyncClient`, клиент S3.
- Эндпоинты получают ресурсы через `Depends`, а не создают их сами. Сессия БД: одна на запрос, открывается и закрывается в зависимости.
- Закрытие в `lifespan` в обратном порядке: сначала перестать принимать работу, потом закрыть клиенты.
- Воркер: по SIGTERM `queue.cancel(consumer_tag)`, дождаться завершения текущих сообщений (с таймаутом), закрыть канал и соединение.
- Контейнеры: `CMD` в exec-форме (сигналы доходят до процесса), `init: true`, `stop_grace_period: 30s`, uvicorn с `--timeout-graceful-shutdown`.
- Healthcheck: `/health/live` (процесс жив) и `/health/ready` (БД, Redis, RabbitMQ доступны). Compose `depends_on` с `condition: service_healthy`.

## 6. Сообщения (RabbitMQ)

- Exchange `forsunki.events` (topic, durable), DLX `forsunki.dlx` (direct, durable).
- Ключи: `<сущность>.<действие>` для событий в прошедшем времени: `user.registered`, `order.created`, `order.paid`, `order.expired`, `repair.requested`. Команды: глагол (`payment.create`).
- Очередь на каждого подписчика, у каждой рабочей очереди есть `.retry` (TTL, возврат через DLX) и `.dlq`. Очереди quorum, durable.
- Отложенные задачи (истечение заказа): очередь с TTL и DLX, без плагинов.
- Сообщение: JSON, persistent, `message_id` (UUID, создаётся один раз и не меняется при повторной отправке), `correlation_id`, `type`, `schema_version`. Изменения схемы только обратно совместимые.
- Publisher: confirms, `mandatory=True`. Consumer: manual ack, prefetch из настроек. Ошибки делим на `TemporaryError` (повтор с backoff, счётчик `x-retry`, лимит) и `PermanentError` (сразу в DLQ). Неизвестное исключение считаем постоянным. В DLQ уходит причина ошибки.
- Топологию объявляет каждый процесс при старте идемпотентно.

## 7. Идемпотентность и защита от двойной оплаты

- **HTTP:** заголовок `Idempotency-Key` на `POST /orders` и других небезопасных POST. Ключ хранится в БД с UNIQUE (Redis только как ускорение), повтор возвращает тот же ответ.
- **Статус заказа:** переход в `PAID` только условным обновлением `UPDATE orders SET status='PAID' WHERE id=:id AND status='PENDING_PAYMENT'`. Если затронуто 0 строк, оплата уже учтена или заказ в другом статусе. Именно это защищает от двойной оплаты.
- **Платежи:** таблица `payments`, провайдерский idempotency key = id заказа + номер попытки, UNIQUE на успешный платёж заказа. Событие webhook'а от провайдера дедуплицируется по `provider_event_id`.
- **Webhook:** проверка подписи HMAC, проверка суммы и валюты, защита от повтора.
- **Consumer (inbox):** `processed_messages(message_id PRIMARY KEY)`, вставка id и бизнес-эффект в **одной** транзакции.
- **Outbox:** событие пишется в `outbox` в той же транзакции, что и бизнес-данные. Relay (в воркере) читает `WHERE sent_at IS NULL ... FOR UPDATE SKIP LOCKED`, публикует с confirms, затем ставит `sent_at`. Дубли возможны, поэтому consumer'ы идемпотентны.
- **Остатки:** резерв только условным `UPDATE products SET stock = stock - :n WHERE id=:id AND stock >= :n`.
- Порядок сообщений не гарантирован: проверяем текущий статус и версию, а не полагаемся на очерёдность.

## 8. Redis

- **Кэш каталога:** cache-aside, ключи с версией, TTL, инвалидация при изменении товара. Защита от cache stampede (блокировка на время прогрева).
- **Сессии `web`:** серверное хранилище, TTL, продление при активности.
- **Rate limiting:** вход, регистрация, форма ремонта (скользящее окно или счётчик с TTL).
- Redis не является источником истины: потеря его содержимого не должна ломать заказы и платежи.

## 9. Логирование

- structlog, JSON в stdout (12-factor). Обязательные поля: `timestamp`, `level`, `service`, `env`, `event`, `correlation_id`. По возможности `order_id`, `user_id`. Пароли, токены, персональные данные и полные данные платежей **не логируем**.
- `correlation_id`: генерируется в `web`/nginx (или принимается из `X-Request-ID`), передаётся в `api` заголовком, в сообщения RabbitMQ, в логи всех процессов.
- **Ротация:** Docker-драйвер `json-file` с `max-size` и `max-file` на каждом контейнере. В контейнерах логи в файлы изнутри приложения не пишем.
- **Сбор:** Fluent Bit читает логи контейнеров и отправляет в два места: Elasticsearch (индекс на день `forsunki-logs-YYYY.MM.DD`, ILM-политика на удаление через N дней) и S3 (gzip, префикс по сервису и дате, lifecycle-правило хранения). В dev S3 эмулирует RustFS.
- **Просмотр:** Kibana с готовым data view и сохранённым дашбордом (ошибки по сервисам, медленные запросы, поиск по `correlation_id`). В prod Kibana не публикуется наружу без аутентификации.
- Стек логирования запускается через compose profile `logging` (Elasticsearch тяжёлый); `make up-full` поднимает всё.

## 10. Метрики

`/metrics` в `api`, `web`, воркере: RPS, ошибки, время ответа (гистограммы), длина очередей и DLQ, количество заказов по статусам, hit ratio кэша. Prometheus + Grafana, дашборд в репозитории.

## 11. Секреты и конфигурация

- Конфигурация через pydantic-settings (переменные окружения). Несекретные настройки в `.env.example`, секретные только через секреты.
- **Runtime:** Docker Compose `secrets:` (файлы монтируются в `/run/secrets/<имя>`). Приложение читает пароль из `<NAME>_FILE` или `secrets_dir`. Образы Postgres и RabbitMQ поддерживают `*_FILE` для начальных учётных данных. Секретов нет в `environment:` и в образах.
- **Build-time:** BuildKit `RUN --mount=type=secret,id=...`. Секреты не передаём через `ARG`/`ENV`, не копируем в слои.
- **Dev:** `make secrets` генерирует случайные значения в `deploy/secrets/dev/` (в `.gitignore`).
- **Prod:** файлы секретов на сервере (права 600, вне репозитория); в CI секреты в GitHub Actions Secrets.
- В CI и pre-commit работает gitleaks. Реальные секреты в репозиторий не попадают никогда.

## 12. Docker и Compose

- Dockerfile: multi-stage, `python:3.12-slim` с зафиксированной версией, зависимости через uv с кэшем, запуск **не от root**, `.dockerignore`, healthcheck. Tailwind собирается отдельной стадией в образе `web`.
- Compose: версии образов зафиксированы, healthchecks на всё, отдельные сети (внешняя, внутренняя, данные), наружу публикуется только nginx (в prod), лимиты ресурсов, `restart: unless-stopped`, именованные тома для данных.
- Файлы: `compose.yml` (база), `compose.dev.yml` (hot reload, проброс портов, Adminer, MailHog, RustFS, Kibana), `compose.prod.yml` (образы из GHCR, без лишних портов, read-only файловая система где возможно, nginx + TLS). Миграции отдельным одноразовым контейнером перед стартом `api`.
- Одна команда запуска: `make up` (ядро) или `make up-full` (с логированием и метриками).

## 13. Безопасность

- Argon2 для паролей. Сессионная cookie: `HttpOnly`, `Secure`, `SameSite`. CSRF-токены для форм в `web`. Заголовки безопасности в nginx.
- Роли (покупатель / admin). Проверка прав в `api`, не только в `web`.
- Валидация всех входов через Pydantic, единый формат ошибок (Problem Details).
- Защита от IDOR: пользователь видит только свои заказы.
- RabbitMQ: `guest` не используется, отдельный пользователь на приложение.

## 14. Тестирование

- Unit: бизнес-логика без сети и БД.
- Интеграционные: Testcontainers с **реальными** Postgres, Redis, RabbitMQ.
- Обязательные сценарии: повторный `Idempotency-Key`; двойной webhook оплаты (деньги и статус учтены один раз); параллельное оформление на последний остаток (не уходим в минус); падение воркера до ack; повтор → DLQ; истечение неоплаченного заказа возвращает остатки; невалидная подпись webhook отклоняется.
- E2E: поднять compose, пройти «регистрация → корзина → заказ → оплата в заглушке → заказ PAID → письмо» через HTTP.
- Новая функциональность без тестов не принимается.

## 15. Команды

```bash
make secrets    # сгенерировать dev-секреты
make up         # ядро: postgres, redis, rabbitmq, api, worker, web, payment_stub
make up-full    # + logging (fluent-bit, elasticsearch, kibana, minio) + monitoring
make down       # остановить
make logs       # логи
make migrate    # миграции
make test       # unit + интеграционные
make e2e        # сквозной сценарий
make lint       # ruff + mypy
make fmt        # ruff format
```

Порты dev: web `8000`, api `8001` (документация `/docs`), RabbitMQ UI `15672`, Adminer `8080`, MailHog `8025`, Kibana `5601`, Grafana `3000`, RustFS `9001`.
Elasticsearch под WSL требует `vm.max_map_count=262144` (описать в README).

## 16. Git и PR

- Ветки `feat/<этап>-<кратко>`, `fix/...`; в `main` напрямую не коммитим. Conventional Commits.
- PR небольшой, тесты зелёные, `make lint` чистый.

## 17. Среда разработки

Windows 11 + WSL: репозиторий хранить в файловой системе WSL (`~/projects/forsunki-shop`), не на `C:\`. Docker Desktop с WSL-интеграцией.

## 18. Как работать с этим проектом (для Claude Code)

- Перед этапом прочитай его раздел в `docs/PLAN.md` и критерии готовности.
- Сначала короткий план изменений, потом код. Если требование неоднозначно, задай вопрос.
- Не добавляй зависимости, сервисы и инструменты вне плана без согласования.
- Неочевидные решения объясняй комментарием или в описании PR (для важных пиши ADR в `docs/adr/`): автор должен уметь пересказать каждое решение на собеседовании.
- Не нарушай разделы 5–8 и 11, даже если так быстрее.
- В конце этапа обнови README и отметь этап в `docs/PLAN.md`.
