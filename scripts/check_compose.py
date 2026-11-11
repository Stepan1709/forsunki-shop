"""Проверка конфигурации Compose: у каждого сервиса настроена ротация Docker-логов и метка сервиса.

Запуск: `make check-compose`. Без `max-size` и `max-file` файл логов контейнера растёт без предела и со временем
занимает весь диск, а без label `com.docker.compose.service` Fluent Bit не узнает, от какого сервиса запись.
"""

import json
import subprocess
import sys

COMMAND = [
    "docker", "compose", "--env-file", ".env",
    "-f", "deploy/compose.yml", "-f", "deploy/compose.dev.yml", "-f", "deploy/compose.load.yml",
    "--profile", "logging", "--profile", "monitoring", "--profile", "tracing", "--profile", "load",
    "config", "--format", "json",
]  # fmt: skip
LABEL = "com.docker.compose.service"


def main() -> int:
    config = json.loads(subprocess.run(COMMAND, check=True, capture_output=True, text=True).stdout)  # noqa: S603
    problems: list[str] = []
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
        print("\n".join(f"  - {p}" for p in problems))
        return 1
    print(f"ok: ротация и метки настроены у всех сервисов ({len(config['services'])})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
