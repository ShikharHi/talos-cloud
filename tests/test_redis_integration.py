"""
Unit and Integration Tests for Upstash / Central Redis Infrastructure and Distributed Locks.
"""

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, patch

from app.infrastructure.redis_client import (
    DistributedLock,
    check_redis_health,
    close_redis_pool,
    distributed_lock,
    get_redis_client,
)
from app.services.rate_limiter import check_rate_limit


@pytest.fixture(autouse=True)
async def cleanup_redis():
    yield
    await close_redis_pool()



@pytest.mark.asyncio
async def test_redis_connection_and_basic_ops():
    client = await get_redis_client()
    if client is None:
        pytest.skip("Live Redis not reachable")

    test_key = f"test:redis_unit:{uuid.uuid4().hex[:8]}"
    await client.set(test_key, "val123", ex=10)
    val = await client.get(test_key)
    ttl = await client.ttl(test_key)
    assert val == "val123"
    assert 0 < ttl <= 10
    await client.delete(test_key)


@pytest.mark.asyncio
async def test_redis_health_check_format():
    health = await check_redis_health()
    assert "status" in health
    assert "tls" in health
    if health["status"] == "healthy":
        assert "latency_ms" in health
        assert isinstance(health["latency_ms"], (int, float))


@pytest.mark.asyncio
async def test_distributed_lock_acquisition_and_release():
    lock_id = f"test_lock_{uuid.uuid4().hex[:6]}"
    async with distributed_lock(lock_id, expire_seconds=5, acquire_timeout_seconds=2.0) as lock:
        assert lock.token is not None

        # Contention test: second lock cannot acquire
        lock2 = DistributedLock(lock_id, expire_seconds=5, acquire_timeout_seconds=0.1)
        acquired = await lock2.acquire()
        assert acquired is False

    # After exit, lock is released: lock3 can acquire
    lock3 = DistributedLock(lock_id, expire_seconds=5, acquire_timeout_seconds=1.0)
    acquired_3 = await lock3.acquire()
    assert acquired_3 is True
    await lock3.release()


@pytest.mark.asyncio
async def test_distributed_lock_auto_expiration():
    lock_id = f"test_expire_{uuid.uuid4().hex[:6]}"
    lock1 = DistributedLock(lock_id, expire_seconds=1, acquire_timeout_seconds=1.0)
    acquired = await lock1.acquire()
    assert acquired is True

    # Wait for TTL expiration
    await asyncio.sleep(1.2)

    # lock2 should now be able to acquire because lock1's key expired
    lock2 = DistributedLock(lock_id, expire_seconds=2, acquire_timeout_seconds=1.0)
    acquired2 = await lock2.acquire()
    assert acquired2 is True
    await lock2.release()


@pytest.mark.asyncio
async def test_rate_limiter_against_upstash_or_fallback():
    key = f"test_rl_{uuid.uuid4().hex[:8]}"
    allowed, headers = await check_rate_limit(key, max_requests=2, window_seconds=5)
    assert allowed is True
    assert headers["X-RateLimit-Limit"] == "2"
    assert headers["X-RateLimit-Remaining"] == "1"

    allowed2, headers2 = await check_rate_limit(key, max_requests=2, window_seconds=5)
    assert allowed2 is True
    assert headers2["X-RateLimit-Remaining"] == "0"

    allowed3, headers3 = await check_rate_limit(key, max_requests=2, window_seconds=5)
    assert allowed3 is False
    assert headers3["X-RateLimit-Remaining"] == "0"
    assert "Retry-After" in headers3
