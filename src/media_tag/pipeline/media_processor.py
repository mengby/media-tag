"""Producer stage 2: multiprocess image downscaling and GPU-accelerated video sampling.

Every task is executed in a process pool; the parent process never decodes pixels,
so CPython's GIL never serialises image/video preparation. Results are JPEG bytes
encoded in memory and base64-encoded for the wire: no PNG round trip, no temporary
thumbnail files.

Video sampling uses FFmpeg in one pass: N chronological frames are arranged into a
single montage JPEG with the ``tile`` filter. NVDEC (``-hwaccel cuda``) is used when
available; ``decoder="auto"`` transparently falls back to software decoding, which
keeps the pipeline usable on machines without a CUDA-capable FFmpeg build.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import subprocess
import time
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

LOGGER = logging.getLogger(__name__)


class MediaProcessingError(RuntimeError):
    """A media file that cannot be prepared for inference inside the fixed budgets."""


def _run_command(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _probe_video(path: Path, options: dict[str, Any]) -> dict[str, Any]:
    command = [
        str(options["ffprobe"]),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        completed = _run_command(command, timeout=30.0)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProcessingError(f"ffprobe failed to start: {exc}") from exc
    if completed.returncode != 0:
        raise MediaProcessingError("ffprobe rejected the video")
    try:
        payload = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise MediaProcessingError("ffprobe did not return JSON") from exc
    streams = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"]
    if not streams:
        raise MediaProcessingError("video has no visual stream")
    stream = streams[0]
    duration = None
    for candidate in (stream.get("duration"), payload.get("format", {}).get("duration")):
        try:
            value = float(candidate)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            duration = value
            break
    return {
        "duration_seconds": duration,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "codec": stream.get("codec_name"),
    }


def _encode_jpeg(image: Image.Image, *, quality: int) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _prepare_image(path: Path, options: dict[str, Any]) -> dict[str, Any]:
    edge = int(options["image_max_edge"])
    quality = int(options["jpeg_quality"])
    budget = int(options["max_image_bytes"])
    started = time.monotonic()
    try:
        with Image.open(path) as opened:
            image = ImageOps.exif_transpose(opened)
            if image.mode in ("RGBA", "LA", "P"):
                # Transparent assets are composited on white so the model sees the
                # same subject it would see in an editor default background.
                background = Image.new("RGB", image.size, (255, 255, 255))
                if image.mode == "P":
                    image = image.convert("RGBA")
                alpha = image.convert("RGBA").getchannel("A")
                background.paste(image.convert("RGB"), mask=alpha)
                image = background
            else:
                image = image.convert("RGB")
            image.thumbnail((edge, edge), Image.Resampling.LANCZOS)
            raw = _encode_jpeg(image, quality=quality)
            attempts = 0
            while len(raw) > budget and attempts < 3:
                attempts += 1
                image.thumbnail((max(64, int(image.width * 0.85)), max(64, int(image.height * 0.85))),
                                Image.Resampling.LANCZOS)
                raw = _encode_jpeg(image, quality=quality)
            if len(raw) > budget:
                raise MediaProcessingError("image_payload_budget")
            width, height = image.size
    except (OSError, ValueError) as exc:
        raise MediaProcessingError(f"image decode failed: {exc}") from exc
    return {
        "ok": True,
        "sha256": options["sha256"],
        "kind": "image",
        "images": [base64.b64encode(raw).decode("ascii")],
        "wire_bytes": len(raw),
        "preprocess_seconds": time.monotonic() - started,
        "meta": {
            "kind": "image",
            "width": width,
            "height": height,
            "jpeg_bytes": len(raw),
            "downscale_attempts": attempts,
            "transform": {"exif_transpose": True, "format": "JPEG", "quality": quality},
        },
    }


def _build_video_filter(edge: int, columns: int, rows: int, samples: int, duration: float) -> str:
    fps = max(samples / max(duration, 0.1), 0.05)
    scale = (
        f"scale=w='min({edge},iw)':h='min({edge},ih)'"
        ":force_original_aspect_ratio=decrease:force_divisible_by=2"
    )
    tile = f"tile={columns}x{rows}:padding=4:margin=0"
    return f"fps={fps:.6f},{scale},setsar=1,{tile}"


def _montage(path: Path, options: dict[str, Any], *, decoder: str, edge: int) -> bytes:
    columns = int(options["video_columns"])
    samples = int(options["video_max_frames"])
    rows = int(math.ceil(samples / columns))
    info = options["_probe"]
    duration = min(float(info["duration_seconds"] or options["video_max_seconds"]),
                   float(options["video_max_seconds"]))
    video_filter = _build_video_filter(edge, columns, rows, samples, duration)
    input_options: list[str] = []
    if decoder == "cuda":
        input_options = ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        video_filter = "hwdownload,format=nv12," + video_filter
    command = [
        str(options["ffmpeg"]),
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *input_options,
        "-ss",
        "0",
        "-i",
        str(path),
        "-t",
        f"{duration:.3f}",
        "-an",
        "-sn",
        "-dn",
    ]
    threads = int(options.get("ffmpeg_threads") or 0)
    if threads > 0:
        command += ["-threads", str(threads)]
    command += [
        "-vf",
        video_filter,
        "-frames:v",
        "1",
        "-q:v",
        str(int(options["video_quality"])),
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        completed = _run_command(command, timeout=120.0)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MediaProcessingError(f"ffmpeg failed to start: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout:
        message = completed.stderr.decode("utf-8", errors="replace")[:300]
        raise MediaProcessingError(f"ffmpeg montage failed: {message}")
    return completed.stdout


def _prepare_video(path: Path, options: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    info = _probe_video(path, options)
    options = {**options, "_probe": info}
    edge = int(options["video_max_edge"])
    budget = int(options["video_max_wire_bytes"])
    requested = str(options["decoder"])
    attempts: list[str] = []
    last_error: Exception | None = None
    for decoder in (("cuda", "cpu") if requested == "auto" else (requested,)):
        attempts.append(decoder)
        try:
            raw = _montage(path, options, decoder=decoder, edge=edge)
        except MediaProcessingError as exc:
            last_error = exc
            continue
        if len(raw) <= budget:
            return _video_result(options, info, raw, decoder, edge, started, attempts)
        # One retry with a smaller canvas before giving up on this decode mode.
        edge = max(512, int(edge * 0.8))
        try:
            raw = _montage(path, options, decoder=decoder, edge=edge)
        except MediaProcessingError as exc:
            last_error = exc
            continue
        if len(raw) <= budget:
            return _video_result(options, info, raw, decoder, edge, started, attempts)
        last_error = MediaProcessingError("video_payload_budget")
    raise last_error or MediaProcessingError("video decode failed")


def _video_result(
    options: dict[str, Any],
    info: dict[str, Any],
    raw: bytes,
    decoder: str,
    edge: int,
    started: float,
    attempts: list[str],
) -> dict[str, Any]:
    columns = int(options["video_columns"])
    samples = int(options["video_max_frames"])
    rows = int(math.ceil(samples / columns))
    duration = min(float(info["duration_seconds"] or options["video_max_seconds"]),
                   float(options["video_max_seconds"]))
    return {
        "ok": True,
        "sha256": options["sha256"],
        "kind": "video",
        "images": [base64.b64encode(raw).decode("ascii")],
        "wire_bytes": len(raw),
        "preprocess_seconds": time.monotonic() - started,
        "meta": {
            "kind": "video",
            "duration_seconds": duration,
            "source_duration_seconds": info["duration_seconds"],
            "source_width": info["width"],
            "source_height": info["height"],
            "source_codec": info["codec"],
            "montage": {
                "columns": columns,
                "rows": rows,
                "cells": samples,
                "frame_order": "left_to_right_then_top_to_bottom",
                "long_edge": edge,
                "jpeg_bytes": len(raw),
            },
            "decoder": decoder,
            "decoder_attempts": attempts,
            "transform": {"format": "JPEG", "fps_sampling": samples / max(duration, 0.1), "audio": "dropped"},
        },
    }


def process_media(task: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Process-pool entry point. Must stay importable and picklable on every platform."""

    path = Path(str(task["path"]))
    merged = {
        **options,
        "sha256": str(task["sha256"]),
        "size_bytes": int(task["size_bytes"]),
    }
    # Cache the probe on the task copy: the montage builder reads it back.
    try:
        if str(task["kind"]) == "image":
            return _prepare_image(path, merged)
        if str(task["kind"]) == "video":
            return _prepare_video(path, merged)
        raise MediaProcessingError(f"unsupported media kind: {task['kind']}")
    except subprocess.TimeoutExpired:
        return {"ok": False, "sha256": str(task["sha256"]), "error": "preprocess_timeout"}
    except MediaProcessingError as exc:
        return {"ok": False, "sha256": str(task["sha256"]), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a single bad file must not kill the pool
        LOGGER.exception("unexpected preprocess failure", extra={"path": str(path)})
        return {"ok": False, "sha256": str(task["sha256"]), "error": f"preprocess_error: {exc}"}
