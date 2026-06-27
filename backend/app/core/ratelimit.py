"""Redis-backed rate limiting for sensitive endpoints (login / OTP / reset).

A fixed-window counter (atomic INCR + EXPIRE) keyed by client IP + scope, stored in the
Redis instance the stack already runs (REDIS_URL). Using Redis means limits are consistent
across restarts and across multiple workers/instances.

Design choice — FAIL OPEN: if Redis is unreachable, requests are allowed (and the error is
logged) rather than blocking all authentication. On a payment platform, a Redis hiccup must
never lock every user out of login; the per-account lockout and per-OTP attempt cap remain as
independent brute-force protections.
"""
import logging
import time
from fastapi import Request, HTTPException
from app.core.config import settings

logger = logging.getLogger("clari5pay.ratelimit")

_redis = None  # lazy singleton async client


async def _client():
    global _redis
    if _redis is None:
        from redis.asyncio import Redis  # lazy import so the app loads even without redis installed
        _redis = Redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
    return _redis


def client_ip(request: Request | None) -> str:
    """Best-effort client IP, honouring a single X-Forwarded-For hop behind nginx/Caddy."""
    if request is None:
        return "unknown"
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _allow(key: str, limit: int, window: int) -> tuple[bool, int]:
    """Returns (allowed, retry_after_seconds). Fails open (allowed=True) on any Redis error."""
    try:
        client = await _client()
        bucket = int(time.time() // window)            # fixed-window bucket id
        rkey = f"rl:{key}:{bucket}"
        count = await client.incr(rkey)
        if count == 1:
            await client.expire(rkey, window + 1)       # first hit sets the TTL
        if count > limit:
            ttl = await client.ttl(rkey)
            return False, ttl if ttl and ttl > 0 else window
        return True, 0
    except Exception as exc:  # Redis down / unreachable → allow, but record it
        logger.warning("[ratelimit] backend error, failing open: %s", exc)
        return True, 0


def rate_limit(limit: int, window: int, scope: str):
    """FastAPI dependency factory — allow `limit` requests per `window` seconds per client IP,
    isolated by `scope`. Over the limit → HTTP 429 (with Retry-After) and a security log line."""
    async def _dep(request: Request) -> None:
        ip = client_ip(request)
        allowed, retry = await _allow(f"{scope}:{ip}", limit, window)
        if not allowed:
            logger.warning(
                "[RATE_LIMITED] scope=%s ip=%s path=%s limit=%d/%ds retry_after=%ds",
                scope, ip, request.url.path, limit, window, retry,
            )
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Please wait a moment and try again.",
                headers={"Retry-After": str(max(1, retry))},
            )
    return _dep
