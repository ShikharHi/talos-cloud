import json
from typing import Optional, Any
from upstash_redis import Redis
from config import config

class RedisCacheService:
    def __init__(self):
        self._client: Optional[Redis] = None
        self._enabled = bool(config.UPSTASH_REDIS_REST_URL and config.UPSTASH_REDIS_REST_TOKEN)

    def _get_client(self) -> Optional[Redis]:
        if not self._enabled:
            return None
        if self._client is None:
            try:
                self._client = Redis(
                    url=config.UPSTASH_REDIS_REST_URL,
                    token=config.UPSTASH_REDIS_REST_TOKEN
                )
            except Exception as e:
                print(f"[Redis Cache] Failed to initialize Upstash Redis client: {e}")
                self._client = None
        return self._client

    def ping(self) -> bool:
        """Check if Redis connection is responsive."""
        client = self._get_client()
        if not client:
            return False
        try:
            return client.ping() == "PONG"
        except Exception:
            return False

    def get_json(self, key: str) -> Optional[Any]:
        """Fetch and deserialize JSON from Redis."""
        client = self._get_client()
        if not client:
            return None
        try:
            val = client.get(key)
            if val:
                if isinstance(val, (dict, list)):
                    return val
                return json.loads(val)
            return None
        except Exception as e:
            print(f"[Redis Cache] Error getting key '{key}': {e}")
            return None

    def set_json(self, key: str, value: Any, ex: int = 3600) -> bool:
        """Serialize and cache JSON with expiration seconds."""
        client = self._get_client()
        if not client:
            return False
        try:
            val_str = json.dumps(value)
            client.set(key, val_str, ex=ex)
            return True
        except Exception as e:
            print(f"[Redis Cache] Error setting key '{key}': {e}")
            return False

    def delete(self, key: str) -> bool:
        """Evict a key from Redis."""
        client = self._get_client()
        if not client:
            return False
        try:
            client.delete(key)
            return True
        except Exception as e:
            print(f"[Redis Cache] Error deleting key '{key}': {e}")
            return False

    def check_rate_limit(self, identifier: str, max_requests: int = 60, window_seconds: int = 60) -> bool:
        """
        Sliding window rate limit using Redis INCR & EXPIRE.
        Returns True if within limit, False if exceeded.
        """
        client = self._get_client()
        if not client:
            return True  # Fail-open if Redis is unavailable
        try:
            key = f"rate_limit:{identifier}"
            count = client.incr(key)
            if count == 1:
                client.expire(key, window_seconds)
            return count <= max_requests
        except Exception:
            return True

redis_cache = RedisCacheService()
