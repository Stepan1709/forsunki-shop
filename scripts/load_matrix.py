"""Серия нагрузочных замеров (`make load`): каталог с кэшем и без него, оформление заказов при 1, 2 и 4 воркерах.

Скрипт сам:
  1. поднимает api с повышенными лимитами регистрации (deploy/compose.load.yml);
  2. запускает Locust в контейнере (профиль load) для каждого замера и читает CSV с итогами;
  3. между замерами масштабирует воркеров и ждёт, пока очереди опустеют, чтобы замеры не влияли друг на друга;
  4. печатает таблицы в формате Markdown и сохраняет их в loadtest/results/summary.md;
  5. в конце возвращает стек в исходный вид (один воркер, штатные лимиты).

Параметры: LOAD_DURATION (секунд на замер, 60), CATALOG_USERS (100), BUYERS (60).
"""

import csv
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "loadtest" / "results"
BASE = [
    "docker",
    "compose",
    "--env-file",
    ".env",
    "-f",
    "deploy/compose.yml",
    "-f",
    "deploy/compose.dev.yml",
]
LOAD = [*BASE, "-f", "deploy/compose.load.yml"]
API = "http://localhost:8001"
DURATION = int(os.environ.get("LOAD_DURATION", "60"))
CATALOG_USERS = int(os.environ.get("CATALOG_USERS", "100"))
BUYERS = int(os.environ.get("BUYERS", "60"))
WORKER_COUNTS = (1, 2, 4)


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, check=check, capture_output=True, text=True)  # noqa: S603


def wait(description: str, check, timeout: float = 180.0) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(2)
    sys.exit(f"не дождались: {description}")


def service_healthy(cmd: list[str], service: str, count: int = 1) -> bool:
    out = run([*cmd, "ps", service, "--format", "{{.Status}}"], check=False).stdout.splitlines()
    return len(out) == count and all("(healthy)" in line for line in out)


def queued_messages() -> int:
    """Сообщения в очередях (готовые и неподтверждённые): ноль значит, что очереди опустели."""
    out = run(
        [*BASE, "exec", "-T", "rabbitmq", "rabbitmqctl", "list_queues", "name", "messages", "-q"]
    ).stdout
    total = 0
    for line in out.splitlines():
        parts = line.split()
        if (
            len(parts) == 2
            and parts[1].isdigit()
            and not parts[0].endswith(".dlq")
            and "expire_delay" not in parts[0]
        ):
            total += int(parts[1])
    return total


def scale_workers(count: int) -> None:
    run([*LOAD, "up", "-d", "--no-deps", "--scale", f"worker={count}", "worker"])
    wait(f"{count} воркер(ов) healthy", lambda: service_healthy(LOAD, "worker", count))


def locust(
    name: str, user_class: str, users: int, spawn_rate: int, cache_mode: str = "warm"
) -> dict[str, dict[str, str]]:
    RESULTS.mkdir(parents=True, exist_ok=True)
    prefix = f"/mnt/locust/results/{name}"
    result = run(
        [
            *LOAD,
            "--profile",
            "load",
            "run",
            "--rm",
            "-e",
            f"CACHE_MODE={cache_mode}",
            "locust",
            "-f",
            "/mnt/locust/locustfile.py",
            "--headless",
            "--only-summary",
            "-u",
            str(users),
            "-r",
            str(spawn_rate),
            "-t",
            f"{DURATION}s",
            "--csv",
            prefix,
            user_class,
        ],
        check=False,
    )
    stats = RESULTS / f"{name}_stats.csv"
    if not stats.exists():
        sys.exit(f"Locust не создал {stats}:\n{result.stdout}\n{result.stderr}")
    with stats.open(encoding="utf-8") as fh:
        return {row["Name"]: row for row in csv.DictReader(fh)}


def register_buyers(count: int) -> None:
    """Регистрирует покупателей заранее и сохраняет их токены для Locust (токен живёт 15 минут, замеров хватает)."""
    password = "load-pass-12345"  # noqa: S105  (тестовый пароль одноразовых пользователей)

    def one(_: int) -> str:
        email = f"load-{uuid.uuid4().hex[:12]}@example.com"
        httpx.post(
            f"{API}/auth/register", json={"email": email, "password": password}, timeout=60
        ).raise_for_status()
        login = httpx.post(
            f"{API}/auth/login", json={"email": email, "password": password}, timeout=60
        )
        return str(login.json()["access_token"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(one, range(count)))
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "buyers.json").write_text(json.dumps(tokens), encoding="utf-8")


def f(row: dict[str, str], key: str) -> float:
    return float(row[key]) if row.get(key) not in (None, "", "N/A") else 0.0


def catalog_table(results: dict[str, dict[str, dict[str, str]]]) -> str:
    lines = [
        "| Запрос | Режим | RPS | p50, мс | p95, мс | Ошибок |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for name in ("GET список", "GET фильтр", "GET поиск", "GET карточка"):
        for mode, label in (("cold", "без кэша"), ("warm", "с кэшем")):
            row = results[mode][name]
            lines.append(
                f"| {name} | {label} | {f(row, 'Requests/s'):.0f} | {f(row, '50%'):.0f} | {f(row, '95%'):.0f} | {row['Failure Count']} |"
            )
    return "\n".join(lines)


def orders_table(results: dict[int, dict[str, dict[str, str]]]) -> str:
    lines = [
        "| Воркеров | Оплачено заказов | Заказов/с | POST /orders p95, мс | Оформление -> PAID p50, с | p95, с | Не дошли до PAID |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for count, rows in results.items():
        e2e = rows.get("E2E checkout -> PAID", {})
        order = rows.get("POST /orders", {})
        paid = int(f(e2e, "Request Count") - f(e2e, "Failure Count"))
        lines.append(
            f"| {count} | {paid} | {paid / DURATION:.2f} | {f(order, '95%'):.0f} "
            f"| {f(e2e, '50%') / 1000:.1f} | {f(e2e, '95%') / 1000:.1f} | {int(f(e2e, 'Failure Count'))} |"
        )
    return "\n".join(lines)


def main() -> None:
    catalog: dict[str, dict[str, dict[str, str]]] = {}
    orders: dict[int, dict[str, dict[str, str]]] = {}
    try:
        # Остатков должно хватать на все замеры: иначе «товар разобрали» исказит результаты. Это dev-данные.
        run([*BASE, "exec", "-T", "postgres", "psql", "-U", "forsunki", "-d", "forsunki", "-c",
             "UPDATE injectors SET stock = 100000 WHERE stock > 0"])  # fmt: skip
        run([*LOAD, "up", "-d", "--no-deps", "--wait", "api"])
        wait("api готов", lambda: service_healthy(LOAD, "api"))

        for mode in ("cold", "warm"):
            print(f"Каталог, {mode}: {CATALOG_USERS} пользователей, {DURATION} с", flush=True)
            catalog[mode] = locust(f"catalog_{mode}", "CatalogUser", CATALOG_USERS, 10, mode)

        register_buyers(BUYERS)
        for count in WORKER_COUNTS:
            print(f"Заказы, воркеров {count}: {BUYERS} покупателей, {DURATION} с", flush=True)
            scale_workers(count)
            wait("очереди пусты", lambda: queued_messages() == 0, 300)
            orders[count] = locust(f"orders_w{count}", "BuyerUser", BUYERS, 20)
            wait("очереди пусты после замера", lambda: queued_messages() == 0, 300)
    finally:
        print("Возвращаем стек в исходный вид", flush=True)
        run([*BASE, "up", "-d", "--no-deps", "--scale", "worker=1", "worker", "api"], check=False)

    summary = (
        f"Параметры: {DURATION} с на замер, каталог {CATALOG_USERS} пользователей, покупателей {BUYERS}.\n\n"
        "Каталог\n\n" + catalog_table(catalog) + "\n\nЗаказы\n\n" + orders_table(orders) + "\n"
    )
    (RESULTS / "summary.md").write_text(summary, encoding="utf-8")
    print("\n" + summary)


if __name__ == "__main__":
    main()
