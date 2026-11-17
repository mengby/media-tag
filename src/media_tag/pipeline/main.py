"""Pipeline orchestration: bounded queues, process pool, async inference pool, micro-batch publish.

Stage layout (all stages run concurrently, connected by bounded queues):

    scanner(thread) ──preprocess_queue(200)──► preprocess stage(12 procs)
        ──inference_queue(512)──► inference workers(32 async tasks, 128 slots)
        ──publish_queue(2048)──► micro-batch publisher (50..100 items / 2s)

The scanner blocks on a full preprocess queue, preprocess submission is capped by
a semaphore of outstanding futures, and inference workers apply weighted slot
admission. That chain is the end-to-end backpressure: memory stays bounded even
when the model server is slower than local preparation.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import queue
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from . import scanner
from .config import DEFAULT_SCHEMA, PipelineConfig, load_config
from .media_processor import process_media
from .metrics import Metrics
from .models import MediaItem, ProcessedItem
from .publisher import MicroBatchPublisher, build_publisher
from .state import State
from .vlm_client import WeightedLimiter, VlmClient, failure_record, load_field_schema

LOGGER = logging.getLogger("media_tag.pipeline")


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _pool_options(config: PipelineConfig) -> dict[str, Any]:
    pre = config.preprocess
    return {
        "image_max_edge": pre.image_max_edge,
        "jpeg_quality": pre.jpeg_quality,
        "max_image_bytes": pre.max_image_bytes,
        "video_max_edge": pre.video_max_edge,
        "video_max_frames": pre.video_max_frames,
        "video_columns": pre.video_columns,
        "video_max_seconds": pre.video_max_seconds,
        "video_max_wire_bytes": pre.video_max_wire_bytes,
        "video_quality": pre.video_quality,
        "decoder": pre.decoder,
        "ffmpeg": pre.ffmpeg,
        "ffprobe": pre.ffprobe,
        "ffmpeg_threads": pre.ffmpeg_threads,
    }


async def _append_jsonl(path: Path, record: dict[str, Any], lock: asyncio.Lock) -> None:
    async with lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


async def _preprocess_stage(
    config: PipelineConfig,
    *,
    preprocess_queue: queue.Queue[MediaItem | None],
    inference_queue: "asyncio.Queue[ProcessedItem | None]",
    pool: ProcessPoolExecutor,
    state: State,
    metrics: Metrics,
    failed_path: Path,
    failed_lock: asyncio.Lock,
) -> None:
    loop = asyncio.get_running_loop()
    options = _pool_options(config)
    inflight = asyncio.Semaphore(config.queues.outstanding_preprocess)
    pending: set[asyncio.Task[None]] = set()

    async def finish(item: MediaItem, result: dict[str, Any]) -> None:
        try:
            if not result.get("ok"):
                error = str(result.get("error") or "preprocess_failed")
                await state.mark(
                    item.sha256,
                    "failed",
                    path=item.path,
                    size_bytes=item.size_bytes,
                    kind=item.kind,
                    error=error,
                    attempt=True,
                )
                await _append_jsonl(
                    failed_path,
                    {
                        "stage": "preprocess",
                        "sha256": item.sha256,
                        "path": str(item.path),
                        "kind": item.kind,
                        "error": error,
                    },
                    failed_lock,
                )
                metrics.incr("failed")
                return
            processed = ProcessedItem(
                media=item,
                images=tuple(result["images"]),
                wire_bytes=int(result["wire_bytes"]),
                meta=dict(result.get("meta") or {}),
                preprocess_seconds=float(result.get("preprocess_seconds") or 0.0),
            )
            await state.mark(
                item.sha256,
                "preprocessed",
                path=item.path,
                size_bytes=item.size_bytes,
                kind=item.kind,
            )
            metrics.incr("preprocessed")
            await inference_queue.put(processed)
        finally:
            inflight.release()

    async def run_one(item: MediaItem) -> None:
        try:
            result = await loop.run_in_executor(pool, process_media, item.as_task(), options)
        except Exception as exc:  # noqa: BLE001 - pool failure is reported per item
            result = {"ok": False, "error": f"preprocess_error: {exc}"}
        await finish(item, result)

    while True:
        item = await loop.run_in_executor(None, preprocess_queue.get)
        if item is None:
            break
        await inflight.acquire()
        task = asyncio.create_task(run_one(item))
        pending.add(task)
        task.add_done_callback(pending.discard)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for _ in range(config.inference.workers):
        await inference_queue.put(None)


async def _inference_worker(
    config: PipelineConfig,
    *,
    inference_queue: "asyncio.Queue[ProcessedItem | None]",
    publish_queue: "asyncio.Queue[Any]",
    vlm: VlmClient,
    state: State,
    metrics: Metrics,
    failed_path: Path,
    failed_lock: asyncio.Lock,
    dry_run: bool,
    publish_enabled: bool,
) -> None:
    while True:
        item = await inference_queue.get()
        if item is None:
            return
        if dry_run:
            metrics.incr("dry_run_items")
            continue
        tagged = await vlm.tag(item)
        if tagged is None:
            record = failure_record(
                item,
                error=str(item.meta.get("_last_error") or "tagging_failed"),
                raw=str(item.meta.get("_last_raw") or ""),
            )
            await _append_jsonl(failed_path, record, failed_lock)
            await state.mark(
                item.media.sha256,
                "failed",
                path=item.media.path,
                kind=item.media.kind,
                error="tagging_failed",
                attempt=True,
            )
            metrics.incr("failed")
            continue
        await state.mark(
            item.media.sha256,
            "inferred",
            path=item.media.path,
            kind=item.media.kind,
            result={"fields": tagged.fields, "model": tagged.model},
        )
        if publish_enabled:
            await publish_queue.put(tagged)


async def _run(
    config: PipelineConfig,
    *,
    limit: int | None,
    dry_run: bool,
    publish_enabled: bool,
) -> int:
    job_dir = config.state.output_dir / config.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    failed_path = job_dir / "failed_records.jsonl"
    receipts_path = job_dir / "receipts.jsonl"

    state = await State(config.state.database).open()
    metrics = Metrics(progress=config.queues.progress and not dry_run)
    preprocess_queue: queue.Queue[MediaItem | None] = queue.Queue(maxsize=config.queues.preprocess)
    inference_queue: asyncio.Queue[ProcessedItem | None] = asyncio.Queue(maxsize=config.queues.inference)
    publish_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=config.queues.publish)
    metrics.bind_queue("preprocess", preprocess_queue.qsize)
    metrics.bind_queue("inference", inference_queue.qsize)
    metrics.bind_queue("publish", publish_queue.qsize)
    failed_lock = asyncio.Lock()
    duplicate_records: list[tuple[str, Path, Path]] = []

    def on_duplicate(sha256: str, path: Path, first: Path) -> None:
        duplicate_records.append((sha256, path, first))
        metrics.incr("duplicates")

    pool = ProcessPoolExecutor(max_workers=config.preprocess.workers)
    inference_done = asyncio.Event()
    reporter = asyncio.create_task(metrics.report(config.queues.metrics_interval_seconds))
    try:
        done_hashes = await state.done_hashes()
        schema = load_field_schema(config.inference.schema_path)
        limits = httpx.Limits(
            max_connections=config.inference.max_connections,
            max_keepalive_connections=config.inference.max_keepalive_connections,
        )
        timeout = httpx.Timeout(config.inference.timeout_seconds)
        async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
            limiter = WeightedLimiter(config.inference.slots)
            vlm = VlmClient(client, config.inference, schema, metrics, limiter=limiter)
            publisher = build_publisher(
                config.publish, job_dir=job_dir, client=client if config.publish.mode == "http" else None
            )
            batch_publisher = MicroBatchPublisher(
                publisher, config.publish, state, metrics, receipts_path=receipts_path
            )
            scan_task = asyncio.create_task(
                asyncio.to_thread(
                    scanner.scan,
                    config.scan,
                    preprocess_queue.put,
                    done_hashes=done_hashes,
                    limit=limit,
                    on_duplicate=on_duplicate,
                )
            )
            pre_task = asyncio.create_task(
                _preprocess_stage(
                    config,
                    preprocess_queue=preprocess_queue,
                    inference_queue=inference_queue,
                    pool=pool,
                    state=state,
                    metrics=metrics,
                    failed_path=failed_path,
                    failed_lock=failed_lock,
                )
            )
            workers = [
                asyncio.create_task(
                    _inference_worker(
                        config,
                        inference_queue=inference_queue,
                        publish_queue=publish_queue,
                        vlm=vlm,
                        state=state,
                        metrics=metrics,
                        failed_path=failed_path,
                        failed_lock=failed_lock,
                        dry_run=dry_run,
                        publish_enabled=publish_enabled,
                    )
                )
                for _ in range(config.inference.workers)
            ]
            publish_task = asyncio.create_task(batch_publisher.run(publish_queue, inference_done))

            summary = await scan_task
            metrics.incr("scanned", summary.queued)
            LOGGER.info(
                "scan finished: seen=%s queued=%s duplicates=%s done=%s unsupported=%s errors=%s",
                summary.files_seen,
                summary.queued,
                summary.duplicates,
                summary.skipped_done,
                summary.skipped_unsupported,
                summary.errors,
            )
            for sha256, path, first in duplicate_records:
                await state.record_duplicate(sha256, path, first)
            await pre_task
            await asyncio.gather(*workers)
            inference_done.set()
            await publish_task

        counts = await state.counts()
        report = metrics.snapshot()
        result = {
            "job_id": config.job_id,
            "schema_version": "pipeline-run-v1",
            "dry_run": dry_run,
            "publish_enabled": publish_enabled,
            "scan": {
                "files_seen": summary.files_seen,
                "queued": summary.queued,
                "duplicates": summary.duplicates,
                "skipped_done": summary.skipped_done,
                "skipped_unsupported": summary.skipped_unsupported,
                "errors": summary.errors,
            },
            "counts": counts,
            "metrics": report,
        }
        (job_dir / "summary.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        LOGGER.info(
            "pipeline finished: published=%s failed=%s summary=%s",
            counts.get("published", 0),
            counts.get("failed", 0),
            job_dir / "summary.json",
        )
        return 0
    finally:
        reporter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reporter
        pool.shutdown(wait=False, cancel_futures=True)
        metrics.close()
        await state.close()


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="media_tag.pipeline",
        description="Local scan -> preprocess -> VLM tagging -> micro-batch publish pipeline.",
    )
    parser.add_argument("--config", type=Path, help="TOML configuration file")
    parser.add_argument("--input", type=Path, action="append", default=[], help="input directory (repeatable)")
    parser.add_argument("--database", type=Path, help="override the SQLite state path")
    parser.add_argument("--output", type=Path, help="override the output directory")
    parser.add_argument("--job-id", default="", help="job identifier; defaults to a timestamp")
    parser.add_argument("--workers", type=int, help="override preprocess process count")
    parser.add_argument("--concurrency", type=int, help="override the inference slot budget")
    parser.add_argument("--limit", type=int, default=None, help="stop after N new media files")
    parser.add_argument("--decoder", choices=("auto", "cuda", "cpu"), help="video decode backend override")
    parser.add_argument("--dry-run", action="store_true", help="scan and preprocess only")
    parser.add_argument("--no-publish", action="store_true", help="run inference but skip publishing")
    parser.add_argument("--log-level", default="", help="DEBUG/INFO/WARNING/ERROR")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    job_id = args.job_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    config_path = args.config or (Path("config") / "pipeline.toml")
    if not config_path.is_file():
        print(
            f"configuration file not found: {config_path}\n"
            "copy config/pipeline.example.toml to config/pipeline.toml and adjust it",
            file=sys.stderr,
        )
        return 2
    config = load_config(config_path, job_id=job_id)
    if args.input:
        config.scan.input_dirs = tuple(path.expanduser() for path in args.input)
    if args.database:
        config.state.database = args.database.expanduser()
    if args.output:
        config.state.output_dir = args.output.expanduser()
    if args.workers:
        config.preprocess.workers = args.workers
    if args.concurrency:
        config.inference.slots = args.concurrency
    if args.decoder:
        config.preprocess.decoder = args.decoder
    if args.log_level:
        config.log_level = args.log_level
    config.validate()
    _configure_logging(config.log_level)
    try:
        return asyncio.run(
            _run(
                config,
                limit=args.limit,
                dry_run=args.dry_run,
                publish_enabled=not args.no_publish,
            )
        )
    except KeyboardInterrupt:
        LOGGER.warning("interrupted; state is persisted and the next run resumes from it")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main", "_run", "DEFAULT_SCHEMA"]
