"""Проверка конфигурации Compose: у каждого сервиса настроена ротация Docker-логов и метка сервиса.

Запуск: `make check-compose`. Без `max-size` и `max-file` файл логов контейнера растёт без предела и со временем
занимает весь диск, а без label `com.docker.compose.service` Fluent Bit не узнает, от какого сервиса запись.
"""

# ruff: noqa: S603  (команда docker compose собрана в этом файле)
import json
import os
import subprocess
import sys

# Две конфигурации: dev (с нагрузочным файлом) и prod. Для prod нужен TAG: значение любое, образы не скачиваются.
COMMANDS = [
    (
        {},
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "deploy/compose.yml",
            "-f",
            "deploy/compose.dev.yml",
            "-f",
            "deploy/compose.load.yml",
            "--profile",
            "logging",
            "--profile",
            "monitoring",
            "--profile",
            "tracing",
            "--profile",
            "load",
            "--profile",
            "backup",
            "config",
            "--format",
            "json",
        ],
    ),
    (
        {"TAG": "check", "SECRETS_DIR": "./secrets/prod"},
        [
            "docker",
            "compose",
            "--env-file",
            ".env",
            "-f",
            "deploy/compose.yml",
            "-f",
            "deploy/compose.prod.yml",
            "--profile",
            "logging",
            "--profile",
            "monitoring",
            "--profile",
            "tracing",
            "--profile",
            "backup",
            "--profile",
            "mailsink",
            "config",
            "--format",
            "json",
        ],
    ),
]
LABEL = "com.docker.compose.service"


def main() -> int:
    problems: list[str] = []
    total = 0
    for extra_env, command in COMMANDS:
        env = {**os.environ, **extra_env}
        config = json.loads(
            subprocess.run(command, check=True, capture_output=True, text=True, env=env).stdout
        )
        total += len(config["services"])
        for name, service in sorted(config["services"].items()):
            logging = service.get("logging") or {}
            options = logging.get("options") or {}
            if logging.get("driver") != "json-file":
                problems.append(f"{name}: драйвер логов не json-file")
            if not options.get("max-size") or not options.get("max-file"):
                problems.append(f"{name}: нет max-size или max-file (ротация)")
            if options.get("labels") != LABEL:
                problems.append(f"{name}: нет labels={LABEL}")
    if problems:
        print("Проблемы с логированием в Compose:")
        for problem in sorted(set(problems)):
            print(f"  - {problem}")
        return 1
    print(f"ok: ротация и метки настроены у всех сервисов (проверено {total} в dev и prod)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
