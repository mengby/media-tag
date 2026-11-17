"""Producer stage 1: one physical read per file, streaming SHA256, in-memory dedupe.

The scanner never decodes media and never writes to the source tree. It only reads
each file once (4 MiB chunks), hashes it while reading, and blocks on a bounded
queue when downstream stages are busy, which is what produces end-to-end
backpressure instead of unbounded buffering.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import ScanConfig
from .models import IMAGE_SUFFIXES, MEDIA_SUFFIXES, VIDEO_SUFFIXES, MediaItem, ScanSummary

LOGGER = logging.getLogger(__name__)
CHUNK_BYTES = 4 * 1024 * 1024
# Windows and macOS system artefacts that are never media even with a matching suffix.
SKIP_DIRS = frozenset(
    ("$RECYCLE.BIN", "System Volume Information", ".Trashes", ".Spotlight-V100", ".fseventsd")
)


@dataclass(slots=True)
class ScanOptions:
    recursive: bool = True
    max_file_bytes: int = 2 * 1024**3
    skip_hidden: bool = True
    follow_symlinks: bool = False
    suffixes: frozenset[str] = MEDIA_SUFFIXES

    @classmethod
    def from_config(cls, config: ScanConfig) -> "ScanOptions":
        suffixes = (
            frozenset(suffix.lower() for suffix in config.include_suffixes)
            if config.include_suffixes
            else MEDIA_SUFFIXES
        )
        return cls(
            recursive=config.recursive,
            max_file_bytes=config.max_file_bytes,
            skip_hidden=config.skip_hidden,
            follow_symlinks=config.follow_symlinks,
            suffixes=suffixes,
        )


def classify(suffix: str) -> str | None:
    value = suffix.lower()
    if value in IMAGE_SUFFIXES:
        return "image"
    if value in VIDEO_SUFFIXES:
        return "video"
    return None


def iter_files(root: Path, options: ScanOptions) -> list[Path]:
    """List candidate files; symlinked files are skipped unless explicitly allowed."""

    if not root.exists():
        raise FileNotFoundError(f"input directory does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"input path is not a directory: {root}")
    found: list[Path] = []
    if options.recursive:
        for directory, dirnames, filenames in os.walk(root, followlinks=options.follow_symlinks):
            if options.skip_hidden:
                dirnames[:] = [
                    name for name in sorted(dirnames)
                    if not name.startswith(".") and name not in SKIP_DIRS
                ]
            else:
                dirnames[:] = [name for name in sorted(dirnames) if name not in SKIP_DIRS]
            for filename in sorted(filenames):
                found.append(Path(directory) / filename)
    else:
        for child in sorted(root.iterdir()):
            if child.is_file():
                found.append(child)
    return found


def hash_file(path: Path, *, max_bytes: int) -> tuple[str, int]:
    """Hash one file in a single sequential read.

    A file that grows past ``max_bytes`` while being read is rejected: the scan is
    snapshot-safety oriented, not a best-effort estimate.
    """

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(CHUNK_BYTES)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(f"file exceeds the scan byte budget: {path}")
            digest.update(chunk)
    return digest.hexdigest(), size


def scan(
    config: ScanConfig,
    put: Callable[[MediaItem | None], None],
    *,
    done_hashes: set[str],
    limit: int | None = None,
    on_duplicate: Callable[[str, Path, Path], None] | None = None,
) -> ScanSummary:
    """Walk every input directory and push unique :class:`MediaItem` values.

    ``put`` is the blocking ``queue.Queue.put`` of the bounded preprocess queue;
    the scanner therefore slows down to the slowest downstream stage. ``None`` is
    pushed exactly once when the walk finishes, also on error.
    """

    options = ScanOptions.from_config(config)
    summary = ScanSummary()
    seen: dict[str, Path] = {}
    try:
        for root in config.input_dirs:
            for path in iter_files(root, options):
                summary.files_seen += 1
                if limit is not None and summary.queued >= limit:
                    LOGGER.info("scan limit reached", extra={"limit": limit})
                    return summary
                suffix = path.suffix.lower()
                if (
                    suffix not in options.suffixes
                    or suffix not in MEDIA_SUFFIXES
                    or path.name.startswith(".")
                    or (options.skip_hidden and path.name.startswith("."))
                    or (path.is_symlink() and not options.follow_symlinks)
                ):
                    summary.skipped_unsupported += 1
                    continue
                kind = classify(suffix)
                if kind is None:
                    summary.skipped_unsupported += 1
                    continue
                try:
                    stat = path.stat()
                    if stat.st_size > options.max_file_bytes:
                        summary.errors += 1
                        LOGGER.warning("file exceeds scan budget", extra={"path": str(path)})
                        continue
                    sha256, size = hash_file(path, max_bytes=options.max_file_bytes)
                except (OSError, ValueError) as exc:
                    summary.errors += 1
                    LOGGER.warning("file could not be read: %s", exc, extra={"path": str(path)})
                    continue
                first = seen.get(sha256)
                if first is not None:
                    summary.duplicates += 1
                    if on_duplicate is not None:
                        on_duplicate(sha256, path, first)
                    continue
                seen[sha256] = path
                if sha256 in done_hashes:
                    summary.skipped_done += 1
                    continue
                put(MediaItem(path=path, sha256=sha256, size_bytes=size, kind=kind, suffix=suffix))
                summary.queued += 1
    finally:
        # The sentinel must be delivered even when the walk fails, otherwise
        # downstream stages would wait forever.
        put(None)
    return summary
