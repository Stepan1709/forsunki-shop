# Контейнер резервного копирования: клиент Postgres той же версии, что и сервер (pg_dump должен быть не старше сервера),
# плюс Python и boto3 для загрузки в S3. Запуск не от root (пользователь postgres из образа).
FROM postgres:18.4-alpine
RUN apk add --no-cache python3 py3-boto3
COPY backup.py /app/backup.py
USER postgres
ENTRYPOINT []
CMD ["python3", "/app/backup.py"]
