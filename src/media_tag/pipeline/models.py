"""Shared dataclasses for the batch pipeline: scan -> preprocess -> tag -> publish."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IMAGE_SUFFIXES = frozenset(
    (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif")
)
VIDEO_SUFFIXES = frozenset(
    (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg", ".ts")
)
MEDIA_SUFFIXES = IMAGE_SUFFIXES | VIDEO_SUFFIXES


@dataclass(slots=True, frozen=True)
class MediaItem:
    """One unique file discovered by :mod:`scanner`; the hash is computed in a single read."""

    path: Path
    sha256: str
    size_bytes: int
    kind: str
    suffix: str

    def as_task(self) -> dict[str, Any]:
        """Picklable form for the process pool."""
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "suffix": self.suffix,
        }


@dataclass(slots=True)
class ProcessedItem:
    """In-memory wire payload: base64 JPEG(s) plus bounded preprocess metadata."""

    media: MediaItem
    images: tuple[str, ...]
    wire_bytes: int
    meta: dict[str, Any] = field(default_factory=dict)
    preprocess_seconds: float = 0.0


@dataclass(slots=True)
class TaggedItem:
    """Validated model output; only these items may enter the publish queue."""

    processed: ProcessedItem
    fields: dict[str, Any]
    raw_response: str
    model: str
    latency_seconds: float
    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(slots=True)
class PublishReceipt:
    sha256: str
    receipt: str


@dataclass(slots=True)
class ScanSummary:
    files_seen: int = 0
    queued: int = 0
    duplicates: int = 0
    skipped_done: int = 0
    skipped_unsupported: int = 0
    errors: int = 0
