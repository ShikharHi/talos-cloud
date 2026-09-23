"""
Talos Cloud — Gateway Concurrency Limiting with Explicit Leases.

Features:
- Per-account concurrent request limits based on subscription tier:
  - free: 2 concurrent requests
  - starter: 5 concurrent requests
  - pro: 10 concurrent requests
  - team: 25 concurrent requests
  - enterprise: 50 concurrent requests
  - admin / unlimited: 100 concurrent requests
- Explicit lease model:
  - acquire_lease(account_id, tier) -> lease_id
  - release_lease(lease_id) -> idempotent, never decrements multiple times
- Distributed lease state in Redis (ZSET of active lease expiration timestamps)
  with automatic TTL/expiry.
- Thread-safe in-memory fallback store with auto-expiration for abandoned leases.
- Full backwards-compatible acquire_concurrency and release_concurrency shims.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
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

DEFAULT_LEASE_TTL_SECONDS = 300.0  # 5 minutes maximum lifetime for a lease


@dataclass
class ConcurrencyLease:
    lease_id: str
    account_id: str
    tier: str
    acquired_at: float
    expires_at: float


class _InMemoryLeaseStore:
    def __init__(self):
        # account_id -> {lease_id: expires_at}
        self._leases: dict[str, dict[str, float]] = defaultdict(dict)
        self._lease_to_account: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def _purge_expired_locked(self, now: float) -> None:
        expired_leases = []
        for lid, acc_id in self._lease_to_account.items():
            exp = self._leases[acc_id].get(lid, 0)
            if exp <= now:
                expired_leases.append((lid, acc_id))

        for lid, acc_id in expired_leases:
            self._leases[acc_id].pop(lid, None)
            self._lease_to_account.pop(lid, None)
            if not self._leases[acc_id]:
                self._leases.pop(acc_id, None)

    async def acquire(self, account_id: str, max_concurrent: int, ttl: float = DEFAULT_LEASE_TTL_SECONDS) -> Optional[str]:
        now = time.time()
        async with self._lock:
            self._purge_expired_locked(now)
            active_count = len(self._leases[account_id])
            if active_count >= max_concurrent:
                return None

            lease_id = str(uuid.uuid4())
            exp = now + ttl
            self._leases[account_id][lease_id] = exp
            self._lease_to_account[lease_id] = account_id
            return lease_id

    async def release(self, lease_id: str) -> bool:
        """Idempotent release: returns True if lease was found and removed, False otherwise."""
        async with self._lock:
            acc_id = self._lease_to_account.pop(lease_id, None)
            if acc_id and acc_id in self._leases:
                self._leases[acc_id].pop(lease_id, None)
                if not self._leases[acc_id]:
                    self._leases.pop(acc_id, None)
                return True
            return False

    async def release_by_account(self, account_id: str) -> None:
        """Fallback for legacy release_concurrency(account_id)."""
        async with self._lock:
            if account_id in self._leases and self._leases[account_id]:
                # Release oldest lease
                oldest_lid = min(self._leases[account_id].keys(), key=lambda k: self._leases[account_id][k])
                self._leases[account_id].pop(oldest_lid, None)
                self._lease_to_account.pop(oldest_lid, None)
                if not self._leases[account_id]:
                    self._leases.pop(account_id, None)

    async def get_count(self, account_id: str) -> int:
        now = time.time()
        async with self._lock:
            self._purge_expired_locked(now)
            return len(self._leases.get(account_id, {}))


_mem_store = _InMemoryLeaseStore()


async def acquire_concurrency_lease(
    account_id: uuid.UUID | str,
    tier: str = "free",
    ttl: float = DEFAULT_LEASE_TTL_SECONDS,
) -> str:
    """
    Acquires an explicit, expiring lease for this stream.
    Raises HTTPException(429) if tier limit is exceeded.
    Returns lease_id string.
    """
    acc_id_str = str(account_id)
    tier_clean = tier.lower() if tier else "free"
    max_concurrent = CONCURRENCY_LIMITS_BY_TIER.get(tier_clean, 2)

    from app.services.rate_limiter import get_redis_client
    redis = await get_redis_client()
    now = time.time()
    lease_id = str(uuid.uuid4())

    if redis:
        try:
            zkey = f"talos:concurrency_leases:{acc_id_str}"
            # 1. Remove expired leases atomically
            await redis.zremrangebyscore(zkey, "-inf", now)
            # 2. Check active count
            active_count = await redis.zcard(zkey)
            if active_count >= max_concurrent:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Concurrent request limit exceeded for tier '{tier}' (max {max_concurrent} active calls)",
                    headers={"Retry-After": "2"},
                )
            # 3. Add lease with score = expiration timestamp
            expires_at = now + ttl
            await redis.zadd(zkey, {lease_id: expires_at})
            await redis.expire(zkey, int(ttl) + 60)
            return lease_id
        except HTTPException:
            raise
        except Exception as e:
            logger.warning("Redis lease acquisition error (%s). Falling back to memory store.", e)
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
        effective_limit = min(max_concurrent, 2) if settings.talos_env == "production" else max_concurrent
        mem_lease = await _mem_store.acquire(acc_id_str, effective_limit, ttl=ttl)
        if not mem_lease:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Concurrent request limit exceeded for tier '{tier}' (max {effective_limit} active calls)",
                headers={"Retry-After": "2"},
            )
        return mem_lease


async def release_concurrency_lease(
    account_id: uuid.UUID | str,
    lease_id: str,
) -> None:
    """
    Idempotent lease release.
    Guaranteed: calling release_concurrency_lease multiple times with the same lease_id
    will NOT decrement other active streams.
    """
    if not lease_id:
        return

    acc_id_str = str(account_id)
    from app.services.rate_limiter import get_redis_client
    redis = await get_redis_client()

    if redis:
        try:
            zkey = f"talos:concurrency_leases:{acc_id_str}"
            await redis.zrem(zkey, lease_id)
            return
        except Exception as e:
            logger.debug("Redis lease release error: %s", e)

    await _mem_store.release(lease_id)


# Legacy backward-compatible methods
async def acquire_concurrency(account_id: uuid.UUID | str, tier: str = "free") -> str:
    """Backwards-compatible acquire: returns lease_id."""
    return await acquire_concurrency_lease(account_id, tier)


async def release_concurrency(account_id: uuid.UUID | str) -> None:
    """Legacy release by account: safely pops one lease if present."""
    acc_id_str = str(account_id)
    from app.services.rate_limiter import get_redis_client
    redis = await get_redis_client()
    now = time.time()

    if redis:
        try:
            zkey = f"talos:concurrency_leases:{acc_id_str}"
            # Pop one expired or oldest lease
            oldest = await redis.zrange(zkey, 0, 0)
            if oldest:
                await redis.zrem(zkey, oldest[0])
            return
        except Exception:
            pass

    await _mem_store.release_by_account(acc_id_str)


@asynccontextmanager
async def check_and_acquire_concurrency(
    account_id: uuid.UUID | str,
    tier: str = "free",
) -> AsyncIterator[str]:
    """Async context manager yielding the acquired lease_id and releasing it on exit."""
    lease_id = await acquire_concurrency_lease(account_id, tier)
    try:
        yield lease_id
    finally:
        await release_concurrency_lease(account_id, lease_id)
