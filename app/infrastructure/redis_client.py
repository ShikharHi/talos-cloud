"""
Talos Cloud — Central Enterprise Redis Operational Infrastructure.

Provides:
- Production-grade connection pooling with TLS support (Upstash & Redis 7+).
- Async client lifecycle management for FastAPI (startup init, graceful shutdown dispose).
- Distributed lock primitive (Redis-backed distributed lock with safe auto-expiration & release token verification).
- Comprehensive health/readiness connectivity check (reports latency, status, never leaks credentials).
- Reusable async redis accessor.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

import redis.asyncio as aioredis
from redis.asyncio.connection import ConnectionPool

from app.config import get_settings

logger = logging.getLogger("talos.infrastructure.redis")

_redis_pool: Optional[ConnectionPool] = None
_redis_client: Optional[aioredis.Redis] = None


def _get_connection_kwargs(url: str) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "decode_responses": True,
        "socket_timeout": 3.0,
        "socket_connect_timeout": 3.0,
        "retry_on_timeout": True,
        "max_connections": 50,
    }
    if url.startswith("rediss://"):
        # Explicit TLS parameters for Upstash managed Redis
        kwargs["ssl_cert_reqs"] = "required"
    return kwargs


async def init_redis_pool() -> Optional[aioredis.Redis]:
    """Initializes the global Redis connection pool and tests connectivity."""
    global _redis_pool, _redis_client
    if _redis_client is not None:
        return _redis_client

    settings = get_settings()
    url = settings.resolved_redis_core_url
    try:
        kwargs = _get_connection_kwargs(url)
        _redis_pool = ConnectionPool.from_url(url, **kwargs)
        client = aioredis.Redis(connection_pool=_redis_pool)
        await client.ping()
        _redis_client = client
        logger.info("Successfully connected to Redis at %s", url.split("@")[-1] if "@" in url else url)
        return _redis_client
    except Exception as exc:
        logger.warning("Failed to connect to operational Redis (%s). Operating in fallback mode.", exc)
        return None


async def close_redis_pool() -> None:
    """Closes the Redis client and connection pool during application shutdown."""
    global _redis_pool, _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception as e:
            logger.warning("Error closing Redis client: %s", e)
        _redis_client = None

    if _redis_pool is not None:
        try:
            await _redis_pool.disconnect()
        except Exception as e:
            logger.warning("Error disconnecting Redis pool: %s", e)
        _redis_pool = None
    logger.info("Redis connection pool disposed.")



async def get_redis_client() -> Optional[aioredis.Redis]:
    """
    Returns the shared async Redis client instance.
    Handles per-event-loop lifecycle cleanly if event loop changes (e.g. in test suites).
    """
    global _redis_client, _redis_pool
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        return None

    if _redis_client is not None:
        client_loop = getattr(_redis_client, "_loop", None)
        if client_loop is not None and (client_loop is not current_loop or client_loop.is_closed()):
            _redis_client = None
            _redis_pool = None
        elif _redis_pool is not None and getattr(_redis_pool, "_loop", None) is not None:
            if _redis_pool._loop is not current_loop or _redis_pool._loop.is_closed():
                _redis_client = None
                _redis_pool = None
        else:
            return _redis_client

    return await init_redis_pool()




async def check_redis_health() -> dict[str, Any]:
    """
    Validates Redis connectivity for readiness checks.
    Measures round-trip latency and returns status without exposing secrets.
    """
    settings = get_settings()
    url = settings.resolved_redis_core_url
    is_tls = url.startswith("rediss://")

    t0 = time.perf_counter()
    client = await get_redis_client()
    if client is None:
        return {
            "status": "unhealthy",
            "tls": is_tls,
            "error": "Redis client unavailable or connection refused",
        }

    try:
        pong = await client.ping()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        if pong:
            return {
                "status": "healthy",
                "tls": is_tls,
                "latency_ms": latency_ms,
            }
        return {
            "status": "unhealthy",
            "tls": is_tls,
            "latency_ms": latency_ms,
            "error": "PING returned false",
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "status": "unhealthy",
            "tls": is_tls,
            "latency_ms": latency_ms,
            "error": str(exc),
        }


# ─── Distributed Lock ──────────────────────────────────────────────────────────

# Lua script to release lock atomically only if token matches (avoids releasing another worker's lock)
RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class DistributedLock:
    """
    Redis-backed distributed lock with automatic TTL expiration to prevent permanent deadlocks
    and safe token verification during release.
    """

    def __init__(
        self,
        name: str,
        expire_seconds: int = 60,
        acquire_timeout_seconds: float = 10.0,
        retry_interval_seconds: float = 0.1,
    ):
        self.name = f"talos:lock:{name}"
        self.expire_seconds = expire_seconds
        self.acquire_timeout_seconds = acquire_timeout_seconds
        self.retry_interval_seconds = retry_interval_seconds
        self.token: Optional[str] = None

    async def acquire(self) -> bool:
        client = await get_redis_client()
        if client is None:
            # Redis is unreachable
            return False

        token = uuid.uuid4().hex
        deadline = time.time() + self.acquire_timeout_seconds

        while time.time() < deadline:
            # SET resource_name my_random_value NX PX milliseconds
            acquired = await client.set(
                self.name,
                token,
                ex=self.expire_seconds,
                nx=True,
            )
            if acquired:
                self.token = token
                return True
            await asyncio.sleep(self.retry_interval_seconds)

        return False

    async def release(self) -> bool:
        if not self.token:
            return False

        client = await get_redis_client()
        if client is None:
            return False

        try:
            res = await client.eval(RELEASE_LOCK_LUA, 1, self.name, self.token)
            self.token = None
            return bool(res == 1)
        except Exception as exc:
            logger.warning("Error releasing distributed lock %s: %s", self.name, exc)
            return False

    async def __aenter__(self) -> DistributedLock:
        acquired = await self.acquire()
        if not acquired:
            raise TimeoutError(f"Could not acquire distributed lock for {self.name}")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.release()


@asynccontextmanager
async def distributed_lock(
    name: str,
    expire_seconds: int = 60,
    acquire_timeout_seconds: float = 10.0,
) -> AsyncIterator[DistributedLock]:
    """Async context manager wrapper for DistributedLock."""
    lock = DistributedLock(
        name=name,
        expire_seconds=expire_seconds,
        acquire_timeout_seconds=acquire_timeout_seconds,
    )
    async with lock:
        yield lock
