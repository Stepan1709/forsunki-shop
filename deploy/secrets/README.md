# Секреты

Реальные значения в репозиторий не попадают. Каталог `dev/` создаётся командой `make secrets`
(случайные значения, игнорируется git). Файлы `*.example` показывают формат.

| Файл | Кто читает |
|---|---|
| `postgres_password` | Postgres (`POSTGRES_PASSWORD_FILE`), приложения |
| `redis_password` | Redis (при старте), приложения |
| `rabbitmq_password` | RabbitMQ (`RABBITMQ_DEFAULT_PASS_FILE`), приложения |
| `jwt_secret` | api (подпись JWT, не короче 32 символов) |
| `s3_access_key`, `s3_secret_key` | S3-хранилища RustFS (root-пользователь) и api (доступ к хранилищу картинок) |

В контейнере файл доступен как `/run/secrets/<имя>`. В prod файлы лежат на сервере вне репозитория
(права 600), путь задаётся переменной `SECRETS_DIR`.
