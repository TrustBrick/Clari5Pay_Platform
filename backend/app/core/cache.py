"""Short-TTL Redis cache for expensive read-only dashboard aggregates.

Fail-open: any Redis error degrades to computing the value directly (never an error). Values are
stored via FastAPI's ``jsonable_encoder`` so a cache hit is exactly what the endpoint would
otherwise return. This is used ONLY for read-side aggregate views (dashboard finance cards,
per-merchant balance/stat rollups) with a TTL of a few seconds — short enough that the figures
stay effectively live. It is NEVER used for financial mutations (deposits, withdrawals,
settlements, approvals), which always hit the database directly.
"""
import json
import logging
from typing import Any, Awaitable, Callable

from fastapi.encoders import jsonable_encoder
from app.core.config import settings

log = logging.getLogger("clari5pay.cache")

_redis = None  # lazy singleton async client (shares the stack's REDIS_URL)


async def _client():
    global _redis
    if _redis is None:
        from redis.asyncio import Redis  # lazy import: app still loads without redis installed
        _redis = Redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
    return _redis


async def cached_json(key: str, ttl: int, compute: Callable[[], Awaitable[Any]]) -> Any:
    """Return the cached value for ``key``; on a miss, run ``compute()``, cache the JSON-encoded
    result for ``ttl`` seconds, and return it. Fail-open: on any Redis error the value is computed
    and returned directly (just uncached), so caching can never break an endpoint."""
    try:
        c = await _client()
        hit = await c.get(key)
        if hit is not None:
            return json.loads(hit)
    except Exception as e:  # Redis down / unreachable → compute directly
        log.warning("[cache] get failed for %s: %s", key, e)
    value = jsonable_encoder(await compute())
    try:
        c = await _client()
        await c.set(key, json.dumps(value), ex=ttl)
    except Exception as e:
        log.warning("[cache] set failed for %s: %s", key, e)
    return value


async def cache_get(key: str):
    """Return the cached value for ``key``, or None on a miss / any Redis error (fail-open).
    Pair with ``cache_set`` for endpoints whose body is a long single-return (cleaner than
    wrapping the whole body in a compute closure)."""
    try:
        c = await _client()
        hit = await c.get(key)
        if hit is not None:
            return json.loads(hit)
    except Exception as e:
        log.warning("[cache] get failed for %s: %s", key, e)
    return None


async def cache_set(key: str, value, ttl: int) -> None:
    """Cache ``value`` (JSON-encoded) under ``key`` for ``ttl`` seconds. Fail-open on any error."""
    try:
        c = await _client()
        await c.set(key, json.dumps(jsonable_encoder(value)), ex=ttl)
    except Exception as e:
        log.warning("[cache] set failed for %s: %s", key, e)
