"""Cache.get_or_load на настоящем Redis: попадания, защита от stampede, сбои."""

import asyncio

import pytest
from fastapi import FastAPI
from redis.asyncio import Redis

from app.core.cache import Cache


@pytest.fixture
def cache(app: FastAPI) -> Cache:
    return Cache(app.state.resources.redis, lock_ttl_seconds=5)


async def test_second_call_does_not_run_loader(cache: Cache) -> None:
    calls = 0

    async def loader() -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": 42}

    first = await cache.get_or_load("k:1", 60, loader)
    second = await cache.get_or_load("k:1", 60, loader)

    assert first == ({"value": 42}, False)
    assert second == ({"value": 42}, True)
    assert calls == 1


async def test_concurrent_misses_run_loader_only_once(cache: Cache) -> None:
    """Cache stampede: 20 одновременных запросов за одним ключом -> одно обращение к источнику."""
    calls = 0

    async def slow_loader() -> dict[str, str]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.3)  # «тяжёлый запрос к БД»
        return {"v": "ready"}

    results = await asyncio.gather(
        *(cache.get_or_load("k:hot", 60, slow_loader) for _ in range(20))
    )

    assert calls == 1
    assert all(value == {"v": "ready"} for value, _ in results)
    assert sum(1 for _, hit in results if not hit) == 1  # один «промах», остальные получили из кэша


async def test_lock_is_released_when_loader_fails(cache: Cache) -> None:
    async def failing() -> dict[str, int]:
        raise RuntimeError("БД упала")

    async def ok() -> dict[str, int]:
        return {"v": 1}

    with pytest.raises(RuntimeError):
        await cache.get_or_load("k:fail", 60, failing)

    # Блокировка снята: следующий запрос не ждёт её TTL, а сразу загружает данные.
    value, hit = await asyncio.wait_for(cache.get_or_load("k:fail", 60, ok), timeout=1)
    assert (value, hit) == ({"v": 1}, False)


async def test_none_result_is_cached_too(cache: Cache) -> None:
    """Несуществующий товар кэшируется (null): повторные запросы к несуществующему id не бьют по БД."""
    calls = 0

    async def loader() -> None:
        nonlocal calls
        calls += 1

    await cache.get_or_load("k:none", 60, loader)
    value, hit = await cache.get_or_load("k:none", 60, loader)

    assert (value, hit) == (None, True)
    assert calls == 1


async def test_version_bump_changes_version(cache: Cache) -> None:
    before = await cache.version()

    await cache.bump_version()

    assert await cache.version() == before + 1


async def test_unavailable_redis_falls_back_to_loader() -> None:
    broken = Cache(Redis.from_url("redis://127.0.0.1:1/0", decode_responses=True))

    async def loader() -> dict[str, int]:
        return {"v": 7}

    assert await broken.get_or_load("k", 60, loader) == ({"v": 7}, False)
    assert await broken.version() == 0
