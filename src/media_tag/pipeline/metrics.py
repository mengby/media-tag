"""Observability: rolling throughput, queue depth and latency, printed as JSON lines."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from typing import Callable

try:  # tqdm is a hard dependency in the pipeline extra, but keep the module importable.
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)
WINDOW_SECONDS = 60.0


class Metrics:
    """Single-event-loop metrics; a few atomically irrelevant counters were not worth locks."""

    def __init__(self, *, progress: bool = True) -> None:
        self.started = time.monotonic()
        self.counters: collections.Counter[str] = collections.Counter()
        self._events: dict[str, collections.deque[float]] = {
            name: collections.deque() for name in ("scanned", "preprocessed", "inferred", "published", "failed")
        }
        self._latencies: collections.deque[float] = collections.deque(maxlen=512)
        self._tokens = collections.Counter()
        self._queue_getters: dict[str, Callable[[], int]] = {}
        self._progress: tqdm | None = None
        if progress and tqdm is not None:
            self._progress = tqdm(total=None, unit="file", dynamic_ncols=True, leave=True)

    def bind_queue(self, name: str, getter: Callable[[], int]) -> None:
        self._queue_getters[name] = getter

    def incr(self, name: str, value: int = 1) -> None:
        self.counters[name] += value
        if name in self._events:
            now = time.monotonic()
            self._events[name].append(now)
            if self._progress is not None and name in ("published", "failed"):
                self._progress.update(value)

    def observe_latency(self, seconds: float) -> None:
        self._latencies.append(seconds)

    def observe_tokens(self, prompt: int, completion: int) -> None:
        self._tokens["prompt"] += prompt
        self._tokens["completion"] += completion

    def _rate(self, name: str) -> float:
        cutoff = time.monotonic() - WINDOW_SECONDS
        events = self._events[name]
        while events and events[0] < cutoff:
            events.popleft()
        return len(events) / WINDOW_SECONDS

    def snapshot(self) -> dict[str, object]:
        elapsed = time.monotonic() - self.started
        latencies = sorted(self._latencies)
        p95 = latencies[int(len(latencies) * 0.95) - 1] if latencies else None
        avg = sum(latencies) / len(latencies) if latencies else None
        return {
            "elapsed_seconds": round(elapsed, 3),
            "per_second": {
                "scan": round(self._rate("scanned"), 3),
                "preprocess": round(self._rate("preprocessed"), 3),
                "infer": round(self._rate("inferred"), 3),
                "publish": round(self._rate("published"), 3),
            },
            "queues": {name: getter() for name, getter in self._queue_getters.items()},
            "inference_latency_seconds": {
                "average": round(avg, 3) if avg is not None else None,
                "p95": round(p95, 3) if p95 is not None else None,
            },
            "counters": dict(self.counters),
            "tokens": dict(self._tokens),
        }

    async def report(self, interval_seconds: float) -> None:
        while True:
            await asyncio.sleep(interval_seconds)
            snapshot = self.snapshot()
            LOGGER.info("%s", json.dumps(snapshot, ensure_ascii=False))
            if self._progress is not None:
                self._progress.set_postfix(
                    published=snapshot["counters"].get("published", 0),
                    failed=snapshot["counters"].get("failed", 0),
                    refresh=False,
                )

    def close(self) -> None:
        if self._progress is not None:
            self._progress.close()
