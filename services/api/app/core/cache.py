"""Кэш Redis по схеме cache-aside с версией ключей и защитой от cache stampede.

Cache-aside: приложение само читает кэш, при промахе идёт в БД и кладёт результат в кэш.
Redis здесь не источник истины: если он недоступен, отвечаем данными из БД (fail-open).

Версия каталога
    Все ключи содержат номер версии (`cat:v7:...`). Изменили товар: увеличили номер (INCR), и все старые ключи
    перестали использоваться, а потом сами исчезают по TTL. Это проще и надёжнее, чем искать и удалять каждый
    затронутый ключ (карточка + все страницы списков, где товар мог оказаться).

Cache stampede
    Когда популярный ключ истёк, десятки запросов одновременно идут в БД за одним и тем же. Защита: кто первым
    взял блокировку (SET NX), тот считает; остальные коротко ждут и читают готовый результат из кэша.
"""

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.metrics import CACHE_REQUESTS

log = structlog.get_logger("cache")

VERSION_KEY = "cat:version"

# Снимаем блокировку только если она всё ещё наша (значение совпадает): если наш TTL истёк и блокировку
# взял другой запрос, обычный DEL удалил бы чужую.
_UNLOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

_WAIT_STEP_SECONDS = 0.05
_WAIT_STEPS = 40  # до 2 секунд ожидания чужого результата


class Cache:
    def __init__(self, redis: Redis, lock_ttl_seconds: int = 10) -> None:
        self._redis = redis
        self._lock_ttl = lock_ttl_seconds

    async def version(self) -> int:
        try:
            raw = await self._redis.get(VERSION_KEY)
            return int(raw) if raw is not None else 0
        except RedisError as exc:
            log.warning("cache_unavailable", op="version", error=type(exc).__name__)
            return 0

    async def bump_version(self) -> None:
        """Сбрасывает весь кэш каталога: вызывается при любом изменении товаров."""
        try:
            await self._redis.incr(VERSION_KEY)
        except RedisError as exc:
            log.warning("cache_unavailable", op="bump_version", error=type(exc).__name__)

    async def get_or_load(
        self,
        key: str,
        ttl_seconds: int,
        loader: Callable[[], Awaitable[Any]],
    ) -> tuple[Any, bool]:
        """Возвращает (значение, из_кэша). `loader` должен возвращать JSON-совместимые данные."""
        try:
            cached = await self._redis.get(key)
            if cached is not None:
                CACHE_REQUESTS.labels("hit").inc()
                return json.loads(cached), True

            lock_key = f"lock:{key}"
            token = str(random.getrandbits(64))
            if await self._redis.set(lock_key, token, nx=True, ex=self._lock_ttl):
                try:
                    value = await loader()
                    await self._redis.set(key, json.dumps(value), ex=self._with_jitter(ttl_seconds))
                    CACHE_REQUESTS.labels("miss").inc()
                    return value, False
                finally:
                    await self._redis.eval(_UNLOCK_SCRIPT, 1, lock_key, token)

            # Блокировку держит другой запрос: ждём, пока он положит результат в кэш.
            for _ in range(_WAIT_STEPS):
                await asyncio.sleep(_WAIT_STEP_SECONDS)
                cached = await self._redis.get(key)
                if cached is not None:
                    CACHE_REQUESTS.labels(
                        "hit"
                    ).inc()  # дождались результата соседнего запроса: БД не нагружена
                    return json.loads(cached), True
        except RedisError as exc:
            log.warning("cache_unavailable", op="get_or_load", error=type(exc).__name__)

        # Redis недоступен или чужой запрос не успел: отвечаем напрямую из БД, не кладя результат в кэш.
        CACHE_REQUESTS.labels("bypass").inc()
        return await loader(), False

    @staticmethod
    def _with_jitter(ttl_seconds: int) -> int:
        # ±10%: ключи, созданные одновременно, истекают в разное время (иначе массовые промахи разом).
        return max(1, int(ttl_seconds * random.uniform(0.9, 1.1)))  # noqa: S311
