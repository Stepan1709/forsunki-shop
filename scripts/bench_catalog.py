"""Замер скорости каталога: без кэша (промахи) и с кэшем (попадания).

Запуск: `make bench` при поднятом стеке (`make up`) и заполненном каталоге (`make seed`).
Каждый замер: N последовательных запросов; для «промахов» перед каждым запросом кэш сбрасывается
служебным эндпоинтом /debug/cache/reset (он есть только вне prod).
"""

import statistics
import sys
import time

import httpx

BASE_URL = "http://localhost:8001"
ITERATIONS = 200

SCENARIOS = {
    "список, 20 шт.": ("/catalog/injectors", {"limit": 20}),
    "фильтр bosch + new": ("/catalog/injectors", {"manufacturer": "bosch", "condition": "new"}),
    "поиск по артикулу": ("/catalog/injectors", {"q": "0445110"}),
    "поиск по двигателю": ("/catalog/injectors", {"q": "om651"}),
    "карточка товара": ("/catalog/injectors/1", {}),
}


def percentile(values: list[float], p: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def measure(
    client: httpx.Client, path: str, params: dict[str, object], *, cold: bool
) -> list[float]:
    timings: list[float] = []
    for _ in range(ITERATIONS):
        if cold:
            client.post("/debug/cache/reset")
        start = time.perf_counter()
        response = client.get(path, params=params)  # type: ignore[arg-type]
        timings.append((time.perf_counter() - start) * 1000)
        expected = "MISS" if cold else "HIT"
        if response.status_code != 200:
            sys.exit(f"{path}: статус {response.status_code}")
        if not cold and response.headers.get("x-cache") != expected and len(timings) > 1:
            sys.exit(f"{path}: ожидался X-Cache: {expected}")
    return timings


def main() -> None:
    rows = []
    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        for name, (path, params) in SCENARIOS.items():
            cold = measure(client, path, params, cold=True)
            client.post("/debug/cache/reset")
            client.get(path, params=params)  # прогрев
            warm = measure(client, path, params, cold=False)
            rows.append((name, cold, warm))

    print(f"\nЗапросов на сценарий: {ITERATIONS}. Время, мс (p50 / p95):\n")
    print(f"{'сценарий':<24}{'без кэша':>18}{'с кэшем':>18}{'ускорение':>12}")
    for name, cold, warm in rows:
        c50, c95 = statistics.median(cold), percentile(cold, 0.95)
        w50, w95 = statistics.median(warm), percentile(warm, 0.95)
        print(f"{name:<24}{c50:>8.1f} /{c95:>7.1f}{w50:>9.1f} /{w95:>7.1f}{c50 / w50:>11.1f}x")


if __name__ == "__main__":
    main()
