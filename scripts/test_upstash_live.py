import asyncio
import os
import sys

from app.infrastructure.redis_client import (
    init_redis_pool,
    check_redis_health,
    close_redis_pool,
    get_redis_client,
    DistributedLock,
)
from app.services.rate_limiter import check_rate_limit

async def main():
    print("--- Testing Upstash Redis Infrastructure ---")
    client = await init_redis_pool()
    assert client is not None, "Failed to connect to Redis pool"
    print("1. Redis client pool connected successfully.")

    health = await check_redis_health()
    print("2. Redis health check:", health)
    assert health["status"] == "healthy"
    assert health["tls"] is True

    # Test key SET / GET / TTL
    await client.set("test_upstash_key", "upstash_works", ex=60)
    val = await client.get("test_upstash_key")
    ttl = await client.ttl("test_upstash_key")
    assert val == "upstash_works"
    assert 0 < ttl <= 60
    print(f"3. Key set/get/ttl verified: val={val}, ttl={ttl}s")
    await client.delete("test_upstash_key")

    # Test Rate Limiting
    rl_key = "ratelimit:test_user:live_upstash"
    allowed, headers = await check_rate_limit(rl_key, max_requests=5, window_seconds=10)
    print("4. Rate limit check headers:", headers)
    assert allowed is True
    assert headers["X-RateLimit-Limit"] == "5"

    # Test Distributed Lock
    async with DistributedLock("integration_test_lock", expire_seconds=10) as lock:
        print(f"5. Distributed lock acquired: {lock.name}")
        # Second attempt should fail or time out
        lock2 = DistributedLock("integration_test_lock", expire_seconds=5, acquire_timeout_seconds=0.2)
        acquired_second = await lock2.acquire()
        assert acquired_second is False, "Contended lock should NOT be acquired"
        print("   Lock contention prevented successfully.")

    # After exiting block, lock must be released
    lock3 = DistributedLock("integration_test_lock", expire_seconds=5, acquire_timeout_seconds=0.5)
    acquired_third = await lock3.acquire()
    assert acquired_third is True, "Lock should be re-acquirable after release"
    await lock3.release()
    print("   Lock released and re-acquired cleanly.")

    await close_redis_pool()
    print("\nALL UPSTASH REDIS INFRASTRUCTURE TESTS PASSED!")

if __name__ == "__main__":
    asyncio.run(main())
