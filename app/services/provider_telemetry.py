"""
Talos Cloud — Provider Health Monitoring & Telemetry (Task 21).

Tracks real-time performance and error metrics per upstream provider:
- Total calls, successes, failures, error rate
- Latency percentiles (p50, p95, p99) over rolling window
- Token throughput (input, output, total)
- Internal dashboard endpoint for platform operators
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
import logging
import statistics
import time
from typing import Any

logger = logging.getLogger("talos.telemetry")


class ProviderTelemetryTracker:
    def __init__(self, sample_window_size: int = 200) -> None:
        self.sample_window_size = sample_window_size
        self._latencies: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=sample_window_size))
        self._success_count: dict[str, int] = defaultdict(int)
        self._failure_count: dict[str, int] = defaultdict(int)
        self._total_tokens: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def record_call(
        self,
        provider: str,
        latency_ms: float,
        success: bool,
        tokens: int = 0,
    ) -> None:
        p = provider.lower().strip()
        async with self._lock:
            self._latencies[p].append(latency_ms)
            if success:
                self._success_count[p] += 1
            else:
                self._failure_count[p] += 1
            self._total_tokens[p] += tokens

    async def get_metrics(self) -> dict[str, Any]:
        """Returns aggregated telemetry per provider."""
        async with self._lock:
            metrics: dict[str, Any] = {}
            all_providers = set(self._latencies.keys()) | set(self._success_count.keys())

            for p in sorted(all_providers):
                samples = list(self._latencies[p])
                succ = self._success_count[p]
                fail = self._failure_count[p]
                total = succ + fail
                err_rate = round((fail / total * 100), 2) if total > 0 else 0.0

                if samples:
                    sorted_samples = sorted(samples)
                    n = len(sorted_samples)
                    p50 = round(sorted_samples[int(n * 0.50)], 2)
                    p95 = round(sorted_samples[min(int(n * 0.95), n - 1)], 2)
                    p99 = round(sorted_samples[min(int(n * 0.99), n - 1)], 2)
                    avg_lat = round(statistics.mean(sorted_samples), 2)
                else:
                    p50 = p95 = p99 = avg_lat = 0.0

                metrics[p] = {
                    "total_calls": total,
                    "success_count": succ,
                    "failure_count": fail,
                    "error_rate_pct": err_rate,
                    "latency_ms": {
                        "avg": avg_lat,
                        "p50": p50,
                        "p95": p95,
                        "p99": p99,
                    },
                    "total_tokens": self._total_tokens[p],
                }

            return metrics


telemetry_tracker = ProviderTelemetryTracker()
