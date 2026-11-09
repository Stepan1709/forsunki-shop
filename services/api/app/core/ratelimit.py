"""Rate limiting на Redis: счётчик с TTL.

Redis здесь не источник истины: если он недоступен, запросы пропускаем (fail-open) и пишем
предупреждение. Сбой кэша не должен делать недоступным вход и регистрацию.
"""

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import TooManyRequestsError

log = structlog.get_logger("ratelimit")

# INCR и EXPIRE одной атомарной операцией: если процесс упадёт между ними, ключ без TTL
# остался бы навсегда и заблокировал пользователя.
_HIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, key: str, window_seconds: int) -> int:
        """Увеличивает счётчик окна и возвращает новое значение."""
        result = await self._redis.eval(_HIT_SCRIPT, 1, f"rl:{key}", window_seconds)
        return int(result)

    async def check(self, key: str, limit: int, window_seconds: int) -> None:
        """Засчитывает попытку и бросает 429, если лимит окна превышен."""
        try:
            count = await self.hit(key, window_seconds)
            if count > limit:
                raise TooManyRequestsError(await self._retry_after(key, window_seconds))
        except RedisError as exc:
            log.warning("ratelimit_unavailable", error=type(exc).__name__)

    async def ensure_not_blocked(self, key: str, limit: int, window_seconds: int) -> None:
        """Бросает 429, если счётчик уже достиг лимита (сам счётчик не меняет)."""
        try:
            raw = await self._redis.get(f"rl:{key}")
            if raw is not None and int(raw) >= limit:
                raise TooManyRequestsError(await self._retry_after(key, window_seconds))
        except RedisError as exc:
            log.warning("ratelimit_unavailable", error=type(exc).__name__)

    async def register_failure(self, key: str, window_seconds: int) -> None:
        try:
            await self.hit(key, window_seconds)
        except RedisError as exc:
            log.warning("ratelimit_unavailable", error=type(exc).__name__)

    async def reset(self, key: str) -> None:
        try:
            await self._redis.delete(f"rl:{key}")
        except RedisError as exc:
            log.warning("ratelimit_unavailable", error=type(exc).__name__)

    async def _retry_after(self, key: str, default: int) -> int:
        ttl = await self._redis.ttl(f"rl:{key}")
        return int(ttl) if ttl and ttl > 0 else default
