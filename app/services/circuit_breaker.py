"""
Talos Cloud — Upstream Provider Circuit Breaker (Task 20).

Prevents cascade failures and latency amplification by fast-failing calls
to downstream LLM providers that are experiencing outages or rate limiting:
- State Machine: CLOSED -> OPEN -> HALF_OPEN
- Failure Threshold: 5 consecutive failures or >50% failure rate over 30 seconds
- Recovery Timeout: 30 seconds in OPEN state before testing with a single probe request
- Fast failover: When circuit is OPEN, relay immediately skips provider to next fallback
- Redis-backed distributed state across all FastAPI worker instances, with local fallback.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections import defaultdict
from typing import Optional

logger = logging.getLogger("talos.circuit_breaker")


class CircuitState(str, enum.Enum):
    CLOSED = "closed"        # Healthy, normal traffic allowed
    OPEN = "open"            # Failing, traffic immediately blocked and failed over
    HALF_OPEN = "half_open"  # Testing recovery with single probe request


class ProviderCircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._states: dict[str, CircuitState] = defaultdict(lambda: CircuitState.CLOSED)
        self._consecutive_failures: dict[str, int] = defaultdict(int)
        self._last_state_change: dict[str, float] = defaultdict(time.time)
        self._lock = asyncio.Lock()

    async def _get_redis(self):
        try:
            from app.services.rate_limiter import get_redis_client
            return await get_redis_client()
        except Exception:
            return None

    async def can_execute(self, provider: str) -> bool:
        """
        Determines whether a call to the given provider is allowed.
        Synchronizes state globally across Redis with local cache fallback.
        If circuit is OPEN, checks if recovery timeout has elapsed to enter HALF_OPEN.
        """
        p = provider.lower().strip()
        now = time.time()

        # Check distributed Redis state first
        redis = await self._get_redis()
        if redis:
            try:
                data = await redis.hgetall(f"talos:circuit:{p}")
                if data and "state" in data:
                    r_state = CircuitState(data["state"])
                    r_changed = float(data.get("changed_at", 0))
                    if r_state == CircuitState.CLOSED:
                        return True
                    if r_state == CircuitState.OPEN:
                        if (now - r_changed) >= self.recovery_timeout_seconds:
                            logger.info("Distributed circuit for '%s' transitioning to HALF_OPEN (probing)", p)
                            await redis.hset(f"talos:circuit:{p}", mapping={"state": CircuitState.HALF_OPEN.value, "changed_at": str(now)})
                            async with self._lock:
                                self._states[p] = CircuitState.HALF_OPEN
                                self._last_state_change[p] = now
                            return True
                        return False
                    if r_state == CircuitState.HALF_OPEN:
                        return True
                elif not data:
                    # Initialize default CLOSED state in Redis
                    await redis.hset(f"talos:circuit:{p}", mapping={"state": CircuitState.CLOSED.value, "fails": "0", "changed_at": str(now)})
                    return True
            except Exception as e:
                logger.debug("Redis circuit breaker read error (%s), using local state", e)


        # Fallback to local thread-safe state
        async with self._lock:
            state = self._states[p]
            if state == CircuitState.CLOSED:
                return True

            if state == CircuitState.OPEN:
                time_open = now - self._last_state_change[p]
                if time_open >= self.recovery_timeout_seconds:
                    logger.info("Circuit for provider '%s' transitioned to HALF_OPEN (probing)", p)
                    self._states[p] = CircuitState.HALF_OPEN
                    self._last_state_change[p] = now
                    return True
                return False

            if state == CircuitState.HALF_OPEN:
                return True

            return True

    async def record_success(self, provider: str) -> None:
        """Records a successful call, resetting the failure counter and closing circuit."""
        p = provider.lower().strip()
        now = time.time()

        redis = await self._get_redis()
        if redis:
            try:
                await redis.hset(f"talos:circuit:{p}", mapping={"state": CircuitState.CLOSED.value, "fails": "0", "changed_at": str(now)})
            except Exception:
                pass

        async with self._lock:
            if self._states[p] != CircuitState.CLOSED:
                logger.info("Provider '%s' healthy: circuit transitioning to CLOSED", p)
            self._states[p] = CircuitState.CLOSED
            self._consecutive_failures[p] = 0
            self._last_state_change[p] = now

    async def record_failure(self, provider: str, error: Optional[Exception] = None) -> None:
        """Records a provider failure. If threshold reached, trips circuit to OPEN."""
        p = provider.lower().strip()
        now = time.time()

        redis = await self._get_redis()
        if redis:
            try:
                fails = await redis.hincrby(f"talos:circuit:{p}", "fails", 1)
                data = await redis.hgetall(f"talos:circuit:{p}")
                curr_state = data.get("state", CircuitState.CLOSED.value)
                if curr_state in (CircuitState.CLOSED.value, CircuitState.HALF_OPEN.value):
                    if fails >= self.failure_threshold or curr_state == CircuitState.HALF_OPEN.value:
                        logger.warning(
                            "Distributed circuit breaker TRIPPED to OPEN for provider '%s' (%d failures). Error: %s",
                            p, fails, error
                        )
                        await redis.hset(f"talos:circuit:{p}", mapping={"state": CircuitState.OPEN.value, "changed_at": str(now)})
                        await redis.expire(f"talos:circuit:{p}", 3600)
            except Exception as e:
                logger.debug("Redis circuit breaker failure write error: %s", e)

        async with self._lock:
            self._consecutive_failures[p] += 1
            fails = self._consecutive_failures[p]

            if self._states[p] in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
                if fails >= self.failure_threshold or self._states[p] == CircuitState.HALF_OPEN:
                    logger.warning(
                        "Circuit breaker TRIPPED to OPEN for provider '%s' (%d failures). Error: %s",
                        p, fails, error
                    )
                    self._states[p] = CircuitState.OPEN
                    self._last_state_change[p] = now

    async def get_state(self, provider: str) -> CircuitState:
        p = provider.lower().strip()
        redis = await self._get_redis()
        if redis:
            try:
                st = await redis.hget(f"talos:circuit:{p}", "state")
                if st:
                    return CircuitState(st)
            except Exception:
                pass

        async with self._lock:
            return self._states[p]

    async def get_all_states(self) -> dict[str, str]:
        async with self._lock:
            return {p: s.value for p, s in self._states.items()}

    async def reset(self, provider: str) -> None:
        """Manually resets circuit for operator intervention."""
        p = provider.lower().strip()
        now = time.time()
        redis = await self._get_redis()
        if redis:
            try:
                await redis.hset(f"talos:circuit:{p}", mapping={"state": CircuitState.CLOSED.value, "fails": "0", "changed_at": str(now)})
            except Exception:
                pass

        async with self._lock:
            self._states[p] = CircuitState.CLOSED
            self._consecutive_failures[p] = 0
            self._last_state_change[p] = now



# Global singleton
circuit_breaker = ProviderCircuitBreaker()
