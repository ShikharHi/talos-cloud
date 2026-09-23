"""
Talos Cloud — SSE Stream Replay Buffer & Sequence Tracker.

Provides:
- Deterministic SSE stream output recovery for transient network drops:
  - Sequence numbers strictly monotonic per event (not per raw chunk boundary).
  - Replay buffer stores complete serialized Talos event bytes out-of-band as a background side effect.
  - In-memory fallback with Redis Sorted Sets (60-second TTL).
- Non-blocking: recording to replay buffer must never delay live delivery to the client.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import logging
import time
from typing import Optional, Tuple

logger = logging.getLogger("talos.stream_buffer")

REPLAY_BUFFER_MAX_CHUNKS = 200
REPLAY_BUFFER_TTL_SECONDS = 60.0


class StreamReplayBuffer:
    def __init__(self) -> None:
        self._buffers: dict[str, deque[Tuple[int, bytes, float]]] = defaultdict(
            lambda: deque(maxlen=REPLAY_BUFFER_MAX_CHUNKS)
        )
        self._seq_counters: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def _get_redis(self):
        try:
            from app.services.rate_limiter import get_redis_client
            return await get_redis_client()
        except Exception:
            return None

    def record_event_background(self, stream_id: str, event_bytes: bytes, seq: int) -> None:
        """Schedules event persistence out-of-band so live stream latency is unaffected."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._record_event_async(stream_id, event_bytes, seq))
        except RuntimeError:
            pass

    async def _record_event_async(self, stream_id: str, event_bytes: bytes, seq: int) -> None:
        now = time.time()
        redis = await self._get_redis()
        if redis:
            try:
                zkey = f"talos:stream:{stream_id}:chunks"
                await redis.zadd(zkey, {event_bytes.decode("utf-8", errors="replace"): float(seq)})
                await redis.expire(zkey, int(REPLAY_BUFFER_TTL_SECONDS))
            except Exception as e:
                logger.debug("Redis replay store error: %s", e)

        async with self._lock:
            self._cleanup_expired(now)
            self._buffers[stream_id].append((seq, event_bytes, now))

    async def next_seq(self, stream_id: str) -> int:
        redis = await self._get_redis()
        if redis:
            try:
                seq = await redis.incr(f"talos:stream:{stream_id}:seq")
                await redis.expire(f"talos:stream:{stream_id}:seq", int(REPLAY_BUFFER_TTL_SECONDS))
                return seq
            except Exception:
                pass
        async with self._lock:
            self._seq_counters[stream_id] += 1
            return self._seq_counters[stream_id]

    # Backward compatibility helper
    async def record_and_format_chunk(self, stream_id: str, raw_chunk: bytes) -> bytes:
        seq = await self.next_seq(stream_id)
        self.record_event_background(stream_id, raw_chunk, seq)
        return raw_chunk

    async def get_replay_chunks(self, stream_id: str, last_event_id: int) -> list[bytes]:
        """Returns all chunks with sequence numbers strictly greater than last_event_id."""
        redis = await self._get_redis()
        if redis:
            try:
                zkey = f"talos:stream:{stream_id}:chunks"
                chunks = await redis.zrangebyscore(zkey, f"({last_event_id}", "+inf")
                if chunks:
                    return [c.encode("utf-8") for c in chunks]
            except Exception as e:
                logger.debug("Redis replay chunk fetch error: %s", e)

        async with self._lock:
            if stream_id not in self._buffers:
                return []
            buf = self._buffers[stream_id]
            return [chunk for seq, chunk, _ in buf if seq > last_event_id]

    def _cleanup_expired(self, now: float) -> None:
        expired_keys = []
        for sid, buf in self._buffers.items():
            if buf and (now - buf[-1][2]) > REPLAY_BUFFER_TTL_SECONDS:
                expired_keys.append(sid)
        for sid in expired_keys:
            self._buffers.pop(sid, None)
            self._seq_counters.pop(sid, None)


# Global singleton
replay_buffer = StreamReplayBuffer()
