"""Consumer stage 2: micro-batched publish with per-item receipts and state updates.

Publishing is never one HTTP call per asset. The consumer accumulates validated
results until ``batch_min`` is reached or ``batch_wait_seconds`` elapsed, then
publishes one batch and confirms every item. A failed batch is retried as a
whole; items that still fail are marked ``failed`` in SQLite so the next run
picks them up without redoing inference.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Protocol

import httpx

from .config import PublishConfig
from .metrics import Metrics
from .models import PublishReceipt, TaggedItem
from .state import State

LOGGER = logging.getLogger(__name__)
SCHEMA_VERSION = "pipeline-publish-v1"


class Publisher(Protocol):
    async def publish(self, batch: list[TaggedItem]) -> list[PublishReceipt]: ...


class LocalJsonlPublisher:
    """Default sink: append-only JSONL inside the job directory (no server contract needed)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    async def publish(self, batch: list[TaggedItem]) -> list[PublishReceipt]:
        receipts: list[PublishReceipt] = []
        with self.path.open("a", encoding="utf-8") as stream:
            for item in batch:
                record = {
                    "schema_version": SCHEMA_VERSION,
                    "sha256": item.processed.media.sha256,
                    "path": str(item.processed.media.path),
                    "kind": item.processed.media.kind,
                    "fields": item.fields,
                    "preprocess": item.processed.meta,
                    "model": item.model,
                    "latency_seconds": round(item.latency_seconds, 3),
                }
                stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                receipts.append(PublishReceipt(sha256=item.processed.media.sha256, receipt="local-jsonl"))
            stream.flush()
        return receipts


class HttpBatchPublisher:
    """HTTP sink. The endpoint contract is provisional until the server adds it.

    Request:  ``{"schema_version": "pipeline-publish-v1", "items": [...]}``
    Response: ``{"accepted": [{"sha256": "...", "receipt": "..."}]}``
    """

    def __init__(self, client: httpx.AsyncClient, config: PublishConfig) -> None:
        self._client = client
        self._config = config
        self._headers = {"Content-Type": "application/json", **config.headers}

    async def publish(self, batch: list[TaggedItem]) -> list[PublishReceipt]:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "items": [
                {
                    "sha256": item.processed.media.sha256,
                    "path": str(item.processed.media.path),
                    "kind": item.processed.media.kind,
                    "fields": item.fields,
                    "preprocess": item.processed.meta,
                }
                for item in batch
            ],
        }
        response = await self._client.post(
            self._config.endpoint, json=payload, headers=self._headers
        )
        response.raise_for_status()
        body = response.json()
        accepted = {
            str(entry.get("sha256")): str(entry.get("receipt") or "")
            for entry in body.get("accepted", [])
            if isinstance(entry, dict)
        }
        missing = [item.processed.media.sha256 for item in batch if item.processed.media.sha256 not in accepted]
        if missing:
            raise ValueError(f"publish response missing receipts for {len(missing)} items")
        return [
            PublishReceipt(sha256=item.processed.media.sha256, receipt=accepted[item.processed.media.sha256])
            for item in batch
        ]


class MicroBatchPublisher:
    def __init__(
        self,
        publisher: Publisher,
        config: PublishConfig,
        state: State,
        metrics: Metrics,
        *,
        receipts_path: Path,
    ) -> None:
        self._publisher = publisher
        self._config = config
        self._state = state
        self._metrics = metrics
        self._receipts_path = receipts_path
        self._receipts_path.parent.mkdir(parents=True, exist_ok=True)

    async def run(self, queue: asyncio.Queue[TaggedItem | None], done: asyncio.Event) -> None:
        loop = asyncio.get_running_loop()
        batch: list[TaggedItem] = []
        deadline: float | None = None
        while True:
            if not batch:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if done.is_set() and queue.empty():
                        break
                    continue
                batch.append(item)  # type: ignore[arg-type]
                deadline = loop.time() + self._config.batch_wait_seconds
                continue
            timeout = max(0.01, (deadline or loop.time()) - loop.time())
            try:
                item = await asyncio.wait_for(queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._flush(batch)
                batch = []
                deadline = None
                continue
            batch.append(item)  # type: ignore[arg-type]
            if len(batch) >= self._config.batch_max:
                await self._flush(batch)
                batch = []
                deadline = None

    async def _flush(self, batch: list[TaggedItem]) -> None:
        if not batch:
            return
        started = asyncio.get_running_loop().time()
        self._metrics.incr("publish_batches")
        for attempt in range(self._config.max_retries):
            try:
                receipts = await self._publisher.publish(batch)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                LOGGER.warning(
                    "publish batch failed (attempt %s/%s): %s",
                    attempt + 1,
                    self._config.max_retries,
                    exc,
                    extra={"batch_size": len(batch)},
                )
                if attempt + 1 < self._config.max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.25))
                continue
            self._append_receipts(receipts)
            for item in batch:
                await self._state.mark(
                    item.processed.media.sha256,
                    "published",
                    path=item.processed.media.path,
                    kind=item.processed.media.kind,
                    result={
                        "fields": item.fields,
                        "model": item.model,
                        "preprocess": item.processed.meta,
                    },
                )
            self._metrics.incr("published", len(batch))
            LOGGER.debug(
                "published batch",
                extra={"batch_size": len(batch), "elapsed": round(asyncio.get_running_loop().time() - started, 3)},
            )
            return
        for item in batch:
            await self._state.mark(
                item.processed.media.sha256,
                "failed",
                path=item.processed.media.path,
                kind=item.processed.media.kind,
                error="publish_failed",
                attempt=True,
            )
        self._metrics.incr("failed", len(batch))
        self._metrics.incr("publish_failed_items", len(batch))

    def _append_receipts(self, receipts: list[PublishReceipt]) -> None:
        with self._receipts_path.open("a", encoding="utf-8") as stream:
            for receipt in receipts:
                stream.write(
                    json.dumps({"sha256": receipt.sha256, "receipt": receipt.receipt}, ensure_ascii=False) + "\n"
                )
            stream.flush()


def build_publisher(
    config: PublishConfig,
    *,
    job_dir: Path,
    client: httpx.AsyncClient | None,
) -> Publisher:
    if config.mode == "http":
        if client is None:
            raise ValueError("HTTP publisher requires a shared HTTP client")
        return HttpBatchPublisher(client, config)
    return LocalJsonlPublisher(job_dir / "published.jsonl")
