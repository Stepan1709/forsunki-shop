"""Резервное копирование Postgres в S3 и проверка восстановления.

Режимы:
  python backup.py            по расписанию: сразу копия, затем раз в BACKUP_INTERVAL_HOURS (контейнер `backup`)
  python backup.py --once     одна копия и выход (make backup)
  python backup.py --verify   восстановить последнюю копию во временную БД и сверить данные (make backup-verify)

Копия это `pg_dump -Fc` (сжатый формат, восстанавливается pg_restore, можно выбрать отдельные таблицы) плюс JSON с версией
миграций и числом строк ключевых таблиц на момент копии. Хранение: старше BACKUP_KEEP_DAYS удаляется.

Резервная копия, которую ни разу не восстанавливали, это надежда, а не копия. Поэтому --verify по-настоящему восстанавливает дамп
в отдельную базу и проверяет: версия миграций совпадает, таблицы на месте, строк не меньше, чем было при копировании
(данные могли только добавиться за время между подсчётом и дампом).
"""

# ruff: noqa: S603, S607  (запускаются фиксированные утилиты postgres, аргументы не приходят снаружи)
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

PREFIX = "postgres/"
CHECK_DB = "forsunki_restore_check"
# Таблицы, по которым сверяем число строк: ключевые данные магазина.
TABLES = ("users", "injectors", "orders", "order_items", "payments")


def secret(name: str) -> str:
    return Path(f"/run/secrets/{name}").read_text(encoding="utf-8").strip()


class Config_:
    pg_host = os.environ.get("PGHOST", "postgres")
    pg_user = os.environ.get("PGUSER", "forsunki")
    pg_db = os.environ.get("PGDATABASE", "forsunki")
    endpoint = os.environ.get("S3_ENDPOINT_URL", "http://s3:9000")
    bucket = os.environ.get("BACKUP_BUCKET", "forsunki-backups")
    region = os.environ.get("S3_REGION", "us-east-1")
    interval_hours = float(os.environ.get("BACKUP_INTERVAL_HOURS", "24"))
    keep_days = float(os.environ.get("BACKUP_KEEP_DAYS", "14"))


def log(event: str, **fields: Any) -> None:
    """JSON в stdout, как у остальных сервисов (его соберёт Fluent Bit)."""
    print(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "service": "backup",
                "event": event,
                **fields,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def s3_client() -> Any:
    return boto3.client(
        "s3",
        endpoint_url=Config_.endpoint,
        region_name=Config_.region,
        aws_access_key_id=secret("s3_access_key"),
        aws_secret_access_key=secret("s3_secret_key"),
        config=Config(s3={"addressing_style": "path"}),
    )


def ensure_bucket(s3: Any) -> None:
    try:
        s3.head_bucket(Bucket=Config_.bucket)
    except ClientError:
        s3.create_bucket(Bucket=Config_.bucket)
        log("bucket_created", bucket=Config_.bucket)


def pg_env() -> dict[str, str]:
    """Пароль передаётся процессам через окружение дочернего процесса: в списке процессов и аргументах его нет."""
    return {**os.environ, "PGPASSWORD": secret("postgres_password")}


def psql(sql: str, db: str | None = None) -> str:
    result = subprocess.run(
        [
            "psql",
            "-h",
            Config_.pg_host,
            "-U",
            Config_.pg_user,
            "-d",
            db or Config_.pg_db,
            "-tAc",
            sql,
        ],
        env=pg_env(),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def table_counts(db: str | None = None) -> dict[str, int]:
    return {t: int(psql(f"SELECT count(*) FROM {t}", db)) for t in TABLES}  # noqa: S608


def expired(objects: list[dict[str, Any]], now: datetime, keep_days: float) -> list[str]:
    """Ключи объектов старше срока хранения."""
    limit = now - timedelta(days=keep_days)
    return [o["Key"] for o in objects if o["LastModified"] < limit]


def backup_once() -> str:
    s3 = s3_client()
    ensure_bucket(s3)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"{PREFIX}forsunki-{stamp}.dump"
    meta = {
        "created_at": stamp,
        "alembic_version": psql("SELECT version_num FROM alembic_version"),
        "counts": table_counts(),
    }
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "db.dump"
        started = time.monotonic()
        subprocess.run(
            [
                "pg_dump",
                "-Fc",
                "-h",
                Config_.pg_host,
                "-U",
                Config_.pg_user,
                "-d",
                Config_.pg_db,
                "-f",
                str(dump),
            ],
            env=pg_env(),
            check=True,
        )
        size = dump.stat().st_size
        s3.upload_file(str(dump), Config_.bucket, key)
    s3.put_object(
        Bucket=Config_.bucket,
        Key=key + ".json",
        Body=json.dumps(meta).encode(),
        ContentType="application/json",
    )
    log("backup_done", key=key, bytes=size, seconds=round(time.monotonic() - started, 1))
    apply_retention(s3)
    return key


def apply_retention(s3: Any) -> None:
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=Config_.bucket, Prefix=PREFIX)
    objects = [o for page in pages for o in page.get("Contents", [])]
    old = expired(objects, datetime.now(UTC), Config_.keep_days)
    for key in old:
        s3.delete_object(Bucket=Config_.bucket, Key=key)
    if old:
        log("backup_retention", deleted=len(old), keep_days=Config_.keep_days)


def latest_dump_key(s3: Any) -> str:
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=Config_.bucket, Prefix=PREFIX)
    dumps = [o for page in pages for o in page.get("Contents", []) if o["Key"].endswith(".dump")]
    if not dumps:
        sys.exit("FAIL  в хранилище нет ни одной копии")
    return str(max(dumps, key=lambda o: o["LastModified"])["Key"])


def verify() -> None:
    s3 = s3_client()
    key = latest_dump_key(s3)
    meta = json.loads(s3.get_object(Bucket=Config_.bucket, Key=key + ".json")["Body"].read())
    print(f"копия: {key}")
    with tempfile.TemporaryDirectory() as tmp:
        dump = Path(tmp) / "db.dump"
        s3.download_file(Config_.bucket, key, str(dump))
        psql(f"DROP DATABASE IF EXISTS {CHECK_DB}", "postgres")
        psql(f"CREATE DATABASE {CHECK_DB}", "postgres")
        try:
            subprocess.run(
                [
                    "pg_restore",
                    "--no-owner",
                    "--exit-on-error",
                    "-h",
                    Config_.pg_host,
                    "-U",
                    Config_.pg_user,
                    "-d",
                    CHECK_DB,
                    str(dump),
                ],
                env=pg_env(),
                check=True,
            )
            restored_version = psql("SELECT version_num FROM alembic_version", CHECK_DB)
            restored = table_counts(CHECK_DB)
        finally:
            psql(f"DROP DATABASE IF EXISTS {CHECK_DB}", "postgres")
    problems = []
    if restored_version != meta["alembic_version"]:
        problems.append(f"версия миграций {restored_version}, ожидалась {meta['alembic_version']}")
    for table, expected in meta["counts"].items():
        if restored[table] < expected:
            problems.append(
                f"{table}: восстановлено {restored[table]}, при копировании было {expected}"
            )
    for table, count in restored.items():
        print(f"  {table:<12} восстановлено {count}, при копировании {meta['counts'][table]}")
    if problems:
        sys.exit("FAIL  " + "; ".join(problems))
    print(f"ok  копия восстанавливается, версия миграций {restored_version}")


def main() -> None:
    if "--verify" in sys.argv:
        verify()
    elif "--once" in sys.argv:
        backup_once()
    else:
        while True:
            try:
                backup_once()
            except Exception as exc:  # цикл не должен умирать от одной неудачной копии
                log("backup_failed", error=type(exc).__name__, detail=str(exc))
            time.sleep(Config_.interval_hours * 3600)


if __name__ == "__main__":
    main()
