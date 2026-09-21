"""
Talos Cloud — SSE Stream Replay Buffer & Sequence Tracker (Task 30).

Provides deterministic SSE stream output recovery:
- Assigns strictly monotonic sequence numbers (`id: {seq}\n`) to outgoing stream chunks.
- Backed by Redis Sorted Sets with 60-second TTL to support cross-instance reconnections
  in multi-replica deployments, with local memory fallback.
- Supports `Last-Event-ID` standard SSE reconnection header: when a client reconnects
  after network interruption, replays missed chunks without re-dispatching to upstream
  provider or double-reserving credits.

IMPORTANT SCOPE NOTE:
This buffer provides best-effort stream *output replay* for transient network drops;
it does NOT checkpoint, resume, or replay the underlying LLM provider generation or agent state.
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
        # Local fallback: task_id -> deque of (seq, chunk_bytes, timestamp)
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

    async def record_and_format_chunk(self, stream_id: str, raw_chunk: bytes) -> bytes:
        """
        Assigns the next sequence number to the chunk and stores it in the replay buffer.
        If the chunk contains SSE data, injects the `id: {seq}\n` header line.
        Saves to distributed Redis when available, otherwise local buffer.
        """
        redis = await self._get_redis()
        seq: int = 0
        chunk_str = raw_chunk.decode("utf-8", errors="replace")

        if redis:
            try:
                seq = await redis.incr(f"talos:stream:{stream_id}:seq")
                await redis.expire(f"talos:stream:{stream_id}:seq", int(REPLAY_BUFFER_TTL_SECONDS))
            except Exception as e:
                logger.debug("Redis seq increment error: %s", e)
                redis = None

        if not redis:
            async with self._lock:
                self._seq_counters[stream_id] += 1
                seq = self._seq_counters[stream_id]

        now = time.time()

        # Format chunk with SSE id if it's SSE data
        if chunk_str.startswith("data:") or "\ndata:" in chunk_str:
            formatted_chunk = f"id: {seq}\n{chunk_str}".encode("utf-8")
        else:
            formatted_chunk = raw_chunk

        # Store in Redis
        if redis:
            try:
                zkey = f"talos:stream:{stream_id}:chunks"
                await redis.zadd(zkey, {formatted_chunk.decode("utf-8", errors="replace"): float(seq)})
                await redis.expire(zkey, int(REPLAY_BUFFER_TTL_SECONDS))
            except Exception as e:
                logger.debug("Redis chunk store error: %s", e)

        # Store in local memory as cache / fallback
        async with self._lock:
            self._cleanup_expired(now)
            self._buffers[stream_id].append((seq, formatted_chunk, now))

        return formatted_chunk

    async def get_replay_chunks(self, stream_id: str, last_event_id: int) -> list[bytes]:
        """
        Returns all chunks with sequence numbers strictly greater than last_event_id.
        Tries Redis first for multi-replica recovery, then falls back to local buffer.
        """
        redis = await self._get_redis()
        if redis:
            try:
                zkey = f"talos:stream:{stream_id}:chunks"
                # Fetch chunks with score > last_event_id
                chunks = await redis.zrangebyscore(zkey, f"({last_event_id}", "+inf")
                if chunks:
                    return [c.encode("utf-8") for c in chunks]
            except Exception as e:
                logger.debug("Redis replay chunk fetch error: %s", e)

        async with self._lock:
            if stream_id not in self._buffers:
                return []

            buf = self._buffers[stream_id]
            replays = [chunk for seq, chunk, _ in buf if seq > last_event_id]
            return replays

    def _cleanup_expired(self, now: float) -> None:
        expired_keys = []
        for sid, buf in self._buffers.items():
            if buf and (now - buf[-1][2]) > REPLAY_BUFFER_TTL_SECONDS:
                expired_keys.append(sid)
        for sid in expired_keys:
            del self._buffers[sid]
            if sid in self._seq_counters:
                del self._seq_counters[sid]


# Global singleton
replay_buffer = StreamReplayBuffer()

