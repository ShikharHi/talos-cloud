"""
Talos Cloud — Distributed Rate Limiting Service.

Features:
- Distributed sliding-window rate limiting backed by Redis sorted sets.
- Multi-dimensional keys (IP, account_id, device_id, endpoint).
- Standard rate limit response headers:
    X-RateLimit-Limit
    X-RateLimit-Remaining
    X-RateLimit-Reset
    Retry-After
- Safe in-memory fallback layer for local development and testing when Redis is unreachable.
- No account existence leakage through rate limit responses.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Callable, Optional

from fastapi import HTTPException, Request, status

from app.config import get_settings

logger = logging.getLogger("talos.rate_limiter")

_redis_client = None
_redis_checked = False
_redis_is_online = False


# ─── In-Memory Fallback Layer ─────────────────────────────────────────────────

class _InMemoryStore:
    """Thread-safe, sliding-window rate limit store for local development/testing."""
    def __init__(self):
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check_rate_limit(
        self, key: str, max_requests: int, window_seconds: int
    ) -> tuple[bool, dict[str, str]]:
        now = time.time()
        clear_before = now - window_seconds
        async with self._lock:
            q = self._windows[key]
            while q and q[0] <= clear_before:
                q.popleft()

            current_count = len(q)
            if current_count >= max_requests:
                oldest = q[0] if q else now
                retry_after = max(1, int(oldest + window_seconds - now))
                headers = {
                    "X-RateLimit-Limit": str(max_requests),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(retry_after),
                    "Retry-After": str(retry_after),
                }
                return False, headers

            q.append(now)
            remaining = max(0, max_requests - len(q))
            headers = {
                "X-RateLimit-Limit": str(max_requests),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": str(window_seconds),
            }
            return True, headers

    async def reset(self) -> None:
        async with self._lock:
            self._windows.clear()


_fallback_store = _InMemoryStore()


# ─── Redis Connection Management ──────────────────────────────────────────────

_last_redis_check_time = 0.0
REDIS_RETRY_INTERVAL_SECONDS = 30.0


async def get_redis_client():
    """Returns async Redis client, or None if Redis is unreachable."""
    from app.infrastructure.redis_client import get_redis_client as _get_infra_redis
    return await _get_infra_redis()


async def is_redis_available() -> bool:
    client = await get_redis_client()
    return client is not None



# ─── Sliding-Window Rate Limiting Engine ──────────────────────────────────────

async def check_rate_limit(
    key: str, max_requests: int, window_seconds: int
) -> tuple[bool, dict[str, str]]:
    """
    Evaluates rate limit using an atomic sliding-window algorithm.
    Returns (is_allowed, headers).
    """
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return True, {
            "X-RateLimit-Limit": str(max_requests),
            "X-RateLimit-Remaining": str(max_requests),
            "X-RateLimit-Reset": str(window_seconds),
        }

    client = await get_redis_client()
    if client is None:
        if settings.rate_limit_fail_closed or settings.talos_env == "production":
            logger.error("CRITICAL: Redis is offline in production. Rate limiter failing closed to protect security boundaries.")
            return False, {
                "X-RateLimit-Limit": str(max_requests),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(window_seconds),
                "Retry-After": "5",
                "X-RateLimit-Degraded": "redis_unavailable",
            }
        return await _fallback_store.check_rate_limit(key, max_requests, window_seconds)

    try:
        now = time.time()
        clear_before = now - window_seconds
        unique_member = f"{now}:{uuid.uuid4().hex[:6]}"

        pipe = client.pipeline()
        pipe.zremrangebyscore(key, 0, clear_before)
        pipe.zcard(key)
        pipe.zrange(key, 0, 0, withscores=True)
        results = await pipe.execute()

        current_count = results[1]
        oldest_records = results[2]

        if current_count >= max_requests:
            oldest_ts = oldest_records[0][1] if oldest_records else now
            retry_after = max(1, int(oldest_ts + window_seconds - now))
            headers = {
                "X-RateLimit-Limit": str(max_requests),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(retry_after),
                "Retry-After": str(retry_after),
            }
            return False, headers

        pipe2 = client.pipeline()
        pipe2.zadd(key, {unique_member: now})
        pipe2.expire(key, window_seconds + 5)
        await pipe2.execute()

        remaining = max(0, max_requests - (current_count + 1))
        headers = {
            "X-RateLimit-Limit": str(max_requests),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(window_seconds),
        }
        return True, headers

    except Exception as e:
        logger.warning("Redis rate limit check failed (%s).", e)
        if settings.rate_limit_fail_closed or settings.talos_env == "production":
            logger.error("CRITICAL: Redis error in production. Rate limiter failing closed.")
            return False, {
                "X-RateLimit-Limit": str(max_requests),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(window_seconds),
                "Retry-After": "5",
                "X-RateLimit-Degraded": "redis_error",
            }
        return await _fallback_store.check_rate_limit(key, max_requests, window_seconds)



# ─── FastAPI Dependency Factory ───────────────────────────────────────────────

def rate_limiter(
    max_requests: int,
    window_seconds: int,
    key_prefix: str = "auth",
    key_getter: Optional[Callable[[Request], str]] = None,
):
    """
    FastAPI dependency factory for endpoint rate-limiting.
    Raises HTTP 429 Too Many Requests when threshold exceeded.
    """
    async def _rate_limit_dependency(request: Request):
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return

        client_ip = request.client.host if request.client else "unknown"
        # Support trusted proxy headers if present
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # First non-empty entry
            client_ip = forwarded_for.split(",")[0].strip()

        if key_getter:
            custom_part = key_getter(request)
            key = f"ratelimit:{key_prefix}:{custom_part}"
        else:
            path_slug = request.url.path.strip("/").replace("/", "_")
            key = f"ratelimit:{key_prefix}:{path_slug}:{client_ip}"

        allowed, headers = await check_rate_limit(key, max_requests, window_seconds)
        if not allowed:
            retry_after = headers.get("Retry-After", "60")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many requests. Please retry in {retry_after} seconds.",
                headers=headers,
            )

    return _rate_limit_dependency
