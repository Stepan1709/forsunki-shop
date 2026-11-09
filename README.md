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
make up        # postgres, redis, rabbitmq, api, worker, S3-хранилище + Adminer, Mailpit
make seed      # заполнить каталог демо-данными (make seed COUNT=20000)
make down
```

| Сервис | Адрес |
|---|---|
| RabbitMQ UI | http://localhost:15672 (пользователь `forsunki`, пароль в `deploy/secrets/dev/rabbitmq_password`) |
| API (Swagger) | http://localhost:8001/docs |
| PostgreSQL | localhost:5433 (порт меняется через `POSTGRES_PUBLISH_PORT` в `.env`) |
| Админка каталога | http://localhost:8001/admin (вход под пользователем с ролью admin) |
| Консоль S3-хранилища | http://localhost:9001 (ключи в `deploy/secrets/dev/s3_*`) |
| Adminer | http://localhost:8080 |
| Mailpit | http://localhost:8025 |

Остальные команды: `make help`. Репозиторий лежит на диске `D:`, команды запускаются из WSL
(`/mnt/d/Opencode_proj/ForsunkiShop`).

Для Elasticsearch (этап 9) в WSL понадобится `sudo sysctl -w vm.max_map_count=262144`.

## Статус

Этапы 0-5 готовы (каркас, ядро `api`, пользователи, каталог и кэш, сообщения, корзина и заказы). Следующий: этап 6, оплата (заглушка).

## Этап 2: авторизация

| Метод и путь | Назначение |
|---|---|
| `POST /auth/register` | регистрация (лимит по IP) |
| `POST /auth/login` | вход, возвращает access (15 мин) и refresh-токен |
| `POST /auth/refresh` | обмен refresh-токена на новую пару (ротация) |
| `POST /auth/logout` | отзыв цепочки refresh-токенов |
| `GET /users/me` | текущий пользователь (нужен `Authorization: Bearer ...`) |

Создать администратора: `make create-admin EMAIL=you@example.com` (пароль вводится в терминале).
Решения и компромиссы: [ADR 0004](docs/adr/0004-avtorizaciya-jwt-i-refresh-tokeny.md).

## Этап 1: что проверено

| Критерий | Результат |
|---|---|
| `docker stop api` во время запроса на 6 с | запрос завершился `200`, остановка 5 с, код выхода 143 |
| `/health/ready` при остановленном Postgres | `503`, `checks.db = fail`; `/health/live` остаётся `200`; после возврата БД снова `200` |
| Логи | JSON в stdout с `timestamp`, `level`, `service`, `env`, `event`, `correlation_id` |
| Ошибки | единый формат Problem Details (`application/problem+json`) |
| Миграции | отдельный контейнер `migrate` (`make migrate`), `upgrade` и `downgrade` проверены тестом |

Тесты ядра идут на настоящих Postgres, Redis и RabbitMQ (Testcontainers), `make test`. Подробности в [docs/PLAN.md](docs/PLAN.md).

## Этап 3: каталог

| Метод и путь | Назначение |
|---|---|
| `GET /catalog/injectors` | список: фильтры `manufacturer`, `type`, `condition`, поиск `q` (артикул или двигатель), курсор `cursor`, `limit` до 100 |
| `GET /catalog/injectors/{id}` | карточка с применимостью к двигателям |
| `GET /catalog/manufacturers` | производители для фильтра |
| `PUT /catalog/injectors/{id}/image` | загрузка картинки (только admin) |
| `/admin` | админка (SQLAdmin): производители, двигатели, форсунки |

Заголовок ответа `X-Cache: HIT` или `MISS` показывает, откуда пришёл ответ. Решения: [ADR 0005](docs/adr/0005-katalog-poisk-i-kesh.md) (поиск, пагинация, кэш) и [ADR 0006](docs/adr/0006-s3-hranilishche-rustfs.md) (картинки и RustFS вместо MinIO).

### Индексы: `EXPLAIN ANALYZE` (18 529 форсунок)

Поиск редкого артикула (`oem_normalized LIKE '%0414720001%' ORDER BY id LIMIT 21`):

| | План | Время |
|---|---|---|
| без триграммного индекса | `Seq Scan on injectors` (отброшено 18 527 строк) | 7,99 мс |
| с GIN-индексом `gin_trgm_ops` | `Bitmap Index Scan on ix_injectors_oem_normalized_trgm` | 0,47 мс |

Выигрыш в 17 раз. Для частого префикса (`%0445110%`, совпадает каждая десятая форсунка) планировщик правильно идёт по первичному ключу и останавливается после 21 совпадения (0,24 мс): индекс помогает там, где совпадений мало. Поиск по двигателю использует `engines` (38 строк) и `injector_engines` (44 880 связей) и выполняется за 0,41 мс.

### Замер кэша (`make bench`, 200 запросов на сценарий, мс p50 / p95)

| Сценарий | Без кэша | С кэшем | Ускорение |
|---|---|---|---|
| список, 20 шт. | 9,8 / 11,7 | 3,7 / 4,7 | 2,6x |
| фильтр bosch + new | 9,9 / 11,9 | 4,0 / 5,1 | 2,5x |
| поиск по артикулу | 12,3 / 19,2 | 3,7 / 4,6 | 3,3x |
| поиск по двигателю | 11,8 / 13,3 | 3,8 / 4,7 | 3,1x |
| карточка товара | 7,9 / 9,5 | 3,8 / 5,7 | 2,1x |

Измерено последовательными запросами с той же машины, где работает БД, поэтому разница скромная (около 4 мс из любого ответа уходит на HTTP и сериализацию). Под нагрузкой с конкурентными запросами выигрыш больше; замер с Locust будет на этапе 11.

## Этап 4: сообщения, outbox и воркер

```
api: транзакция {бизнес-данные + строка outbox}  ->  worker/relay: SELECT ... FOR UPDATE SKIP LOCKED
   -> публикация с confirms (forsunki.events) -> очередь подписчика -> consumer: inbox + обработчик + commit -> ack
        ошибка TemporaryError -> ярус .retry.k (пауза 5/30/120 с) -> снова в очередь
        PermanentError, лимит повторов -> .dlq с причиной в заголовках
```

| Гарантия | Чем обеспечена |
|---|---|
| Событие не теряется, если сохранены данные | outbox в одной транзакции с данными |
| Событие не публикуется, если данные откатились | то же: откат убирает и строку outbox |
| Сообщение не теряется, если воркер упал до ack | ручной ack, брокер возвращает сообщение другому воркеру |
| Дубль не даёт двойного эффекта | inbox: запись и эффект в одной транзакции |
| Ядовитое сообщение не зацикливает очередь | лимит повторов, `x-delivery-limit`, DLQ |
| Остановка не теряет сообщения в работе | cancel consumer'ов, ожидание текущих, затем закрытие |

Решения и компромиссы: [ADR 0007](docs/adr/0007-soobscheniya-outbox-inbox-retry.md). Топологию RabbitMQ и содержимое DLQ
можно смотреть в веб-интерфейсе: http://localhost:15672 (пользователь `forsunki`, пароль в `deploy/secrets/dev/rabbitmq_password`).

## Этап 5: корзина и заказы

| Метод и путь | Назначение |
|---|---|
| `GET /cart`, `PUT /cart/items/{id}`, `DELETE /cart/items/{id}`, `DELETE /cart` | корзина (количество 1-99, не больше остатка) |
| `POST /orders` (`Idempotency-Key`) | оформить заказ из корзины |
| `GET /orders`, `GET /orders/{id}` | история (от новых к старым, курсор) и карточка; только свои заказы |
| `POST /orders/{id}/cancel` (`Idempotency-Key`) | отменить неоплаченный заказ |

Оформление одной транзакцией: резерв остатков условным `UPDATE ... WHERE stock >= n`, заказ `PENDING_PAYMENT` со снимком цен, очистка
корзины, событие `order.created`. Что проверено (все тесты на реальных Postgres и RabbitMQ):

| Сценарий | Результат |
|---|---|
| повтор `POST /orders` с тем же ключом | тот же заказ, резерв и событие один раз, заголовок `Idempotent-Replayed: true` |
| 6 одновременных запросов с одним ключом | один заказ |
| 8 покупателей одновременно на последнюю единицу | один заказ (201), семь отказов (409), остаток 0, не минус |
| нехватка одной позиции из нескольких | заказ не создан, остатки всех позиций не тронуты |
| одновременные «отмена» и «истёк» | статус сменил один, товар вернулся один раз |
| случайные последовательности действий (Hypothesis, реальная БД) | остаток + зарезервированное + проданное = начальный запас |
| чужой заказ | 404 |

Статусы: `PENDING_PAYMENT → PAID | PAYMENT_FAILED | CANCELLED | EXPIRED` (остальные конечные); недопустимый переход возвращает 409.
Решения: [ADR 0008](docs/adr/0008-zakazy-idempotentnost-i-rezerv.md).
