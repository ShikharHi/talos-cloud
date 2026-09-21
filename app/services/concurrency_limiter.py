"""
Talos Cloud — Gateway Concurrency Limiting (Task 18).

Enforces per-account concurrent request limits based on subscription tier:
- free: 2 concurrent requests
- starter: 5 concurrent requests
- pro: 10 concurrent requests
- team: 25 concurrent requests
- enterprise: 50 concurrent requests
- admin / unlimited: 100 concurrent requests

Distributed state in Redis with atomic INCR/DECR and automatic TTL-based expiry
(to prevent worker crash leaks), with thread-safe in-memory fallback for local dev.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import HTTPException, status

logger = logging.getLogger("talos.concurrency_limiter")

CONCURRENCY_LIMITS_BY_TIER: dict[str, int] = {
    "free": 2,
    "starter": 5,
    "pro": 10,
    "team": 25,
    "enterprise": 50,
    "admin": 100,
    "unlimited": 100,
}

DEFAULT_IN_FLIGHT_TTL_SECONDS = 300  # Auto-expire in 5 min to protect against crashed workers


class _InMemoryConcurrencyStore:
    def __init__(self):
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def acquire(self, account_id: str, max_concurrent: int) -> bool:
        async with self._lock:
            current = self._counts[account_id]
            if current >= max_concurrent:
                return False
            self._counts[account_id] = current + 1
            return True

    async def release(self, account_id: str) -> None:
        async with self._lock:
            if account_id in self._counts:
                self._counts[account_id] = max(0, self._counts[account_id] - 1)
                if self._counts[account_id] == 0:
                    del self._counts[account_id]

    async def get_count(self, account_id: str) -> int:
        async with self._lock:
            return self._counts.get(account_id, 0)


_mem_store = _InMemoryConcurrencyStore()


async def acquire_concurrency(account_id: uuid.UUID | str, tier: str = "free") -> None:
    """
    Acquires a concurrency slot for the given account and tier.
    Raises HTTPException(429) if tier limit is exceeded.
    """
    acc_id_str = str(account_id)
    max_concurrent = CONCURRENCY_LIMITS_BY_TIER.get(tier.lower(), 2)

    from app.services.rate_limiter import get_redis_client
    redis = await get_redis_client()

    key = f"talos:concurrency:{acc_id_str}"

    if redis:
        try:
            current = await redis.incr(key)
            if current == 1:
                await redis.expire(key, DEFAULT_IN_FLIGHT_TTL_SECONDS)
            if current > max_concurrent:
                await redis.decr(key)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Concurrent request limit exceeded for tier '{tier}' (max {max_concurrent} active calls)",
                    headers={"Retry-After": "2"},
                )
            return
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Redis concurrency check error (%s).", e)
            redis = None

    if not redis:
        from app.config import get_settings
        settings = get_settings()
        if settings.concurrency_fail_closed:
            logger.error("CRITICAL: Redis concurrency check failed in fail-closed mode.")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Gateway concurrency service temporarily unavailable.",
                headers={"Retry-After": "3"},
            )
        # In production with Redis down, enforce strict conservative degraded mode (at most 2 concurrent calls per instance)
        effective_limit = min(max_concurrent, 2) if settings.talos_env == "production" else max_concurrent
        if settings.talos_env == "production":
            logger.warning(
                "CRITICAL: Redis unavailable in production. Enforcing degraded per-instance cap of %d active calls for account %s.",
                effective_limit, acc_id_str
            )
        allowed = await _mem_store.acquire(acc_id_str, effective_limit)
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Concurrent request limit exceeded for tier '{tier}' (max {effective_limit} active calls)",
                headers={"Retry-After": "2"},
            )



async def release_concurrency(account_id: uuid.UUID | str) -> None:
    """Releases a concurrency slot."""
    acc_id_str = str(account_id)
    from app.services.rate_limiter import get_redis_client
    redis = await get_redis_client()
    key = f"talos:concurrency:{acc_id_str}"

    if redis:
        try:
            await redis.decr(key)
            return
        except Exception:
            pass
    await _mem_store.release(acc_id_str)


@asynccontextmanager
async def check_and_acquire_concurrency(
    account_id: uuid.UUID | str,
    tier: str = "free",
) -> AsyncIterator[None]:
    """
    Async context manager guarding execution against per-account tier limits.
    Raises HTTPException(429) if limit is exceeded.
    """
    await acquire_concurrency(account_id, tier)
    try:
        yield
    finally:
        await release_concurrency(account_id)

