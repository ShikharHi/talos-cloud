"""
Unit tests for Talos Cloud Distributed Rate Limiter (Task 5).
"""

import asyncio
import time
import pytest
from fastapi import HTTPException, Request

from app.services.rate_limiter import (
    _InMemoryStore,
    check_rate_limit,
    rate_limiter,
)


@pytest.mark.asyncio
async def test_in_memory_store_allows_within_limit():
    store = _InMemoryStore()
    key = "test_client_1"
    
    for _ in range(5):
        allowed, headers = await store.check_rate_limit(key, max_requests=5, window_seconds=60)
        assert allowed is True
        assert int(headers["X-RateLimit-Limit"]) == 5
        assert int(headers["X-RateLimit-Remaining"]) >= 0

    # 6th request should be blocked
    allowed, headers = await store.check_rate_limit(key, max_requests=5, window_seconds=60)
    assert allowed is False
    assert headers["X-RateLimit-Remaining"] == "0"
    assert "Retry-After" in headers
    assert int(headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_in_memory_store_sliding_window_expiration():
    store = _InMemoryStore()
    key = "test_client_expiry"
    
    # 2 requests with 1 second window
    allowed, _ = await store.check_rate_limit(key, max_requests=2, window_seconds=1)
    assert allowed is True
    allowed, _ = await store.check_rate_limit(key, max_requests=2, window_seconds=1)
    assert allowed is True
    
    # 3rd request blocked
    allowed, _ = await store.check_rate_limit(key, max_requests=2, window_seconds=1)
    assert allowed is False
    
    # Wait for window to expire
    await asyncio.sleep(1.1)
    
    # Should now be allowed again
    allowed, headers = await store.check_rate_limit(key, max_requests=2, window_seconds=1)
    assert allowed is True
    assert headers["X-RateLimit-Remaining"] == "1"


@pytest.mark.asyncio
async def test_rate_limiter_dependency_enforcement():
    limiter = rate_limiter(max_requests=3, window_seconds=60, key_prefix="test_dep")
    
    # Construct mock request
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/test",
        "headers": [(b"host", b"testserver")],
        "client": ("192.168.1.50", 12345),
    }
    req = Request(scope)
    
    # 3 requests pass
    for _ in range(3):
        await limiter(req)
        
    # 4th request raises 429
    with pytest.raises(HTTPException) as exc_info:
        await limiter(req)
        
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers is not None
    assert "Retry-After" in exc_info.value.headers
    assert exc_info.value.headers["X-RateLimit-Remaining"] == "0"
