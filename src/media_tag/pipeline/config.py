"""Pipeline configuration: TOML file plus explicit CLI overrides, no ambient defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_SCHEMA = Path(__file__).with_name("schema") / "tag_fields.schema.json"


@dataclass(slots=True)
class ScanConfig:
    input_dirs: tuple[Path, ...] = ()
    recursive: bool = True
    max_file_bytes: int = 2 * 1024**3
    skip_hidden: bool = True
    follow_symlinks: bool = False
    # Empty means the built-in image + video suffix policy.
    include_suffixes: tuple[str, ...] = ()


@dataclass(slots=True)
class PreprocessConfig:
    workers: int = 12
    image_max_edge: int = 896
    jpeg_quality: int = 85
    max_image_bytes: int = 150 * 1024
    video_max_edge: int = 1280
    video_max_frames: int = 9
    video_columns: int = 3
    video_max_seconds: float = 120.0
    video_max_wire_bytes: int = 400 * 1024
    video_quality: int = 3
    decoder: str = "auto"  # auto | cuda | cpu
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    ffmpeg_threads: int = 0  # 0 lets FFmpeg decide; >0 pins -threads

    def validate(self) -> None:
        if self.workers < 1:
            raise ValueError("preprocess.workers must be positive")
        if self.decoder not in ("auto", "cuda", "cpu"):
            raise ValueError("preprocess.decoder must be auto, cuda or cpu")
        if not 256 <= self.image_max_edge <= 4096:
            raise ValueError("preprocess.image_max_edge out of range")
        if not 1 <= self.video_max_frames <= 9:
            raise ValueError("preprocess.video_max_frames must be 1..9 for a 3x3 montage")
        if not 1 <= self.video_columns <= 3:
            raise ValueError("preprocess.video_columns must be 1..3")


@dataclass(slots=True)
class InferenceConfig:
    base_url: str = "http://127.0.0.1:18080/v1"
    model: str = "models"
    api_key_env: str = "MEDIA_TAG_AUTODL_API_KEY"
    slots: int = 128
    image_slots: int = 1
    video_slots: int = 2
    workers: int = 32
    timeout_seconds: float = 60.0
    max_retries: int = 3
    temperature: float = 0.1
    max_tokens: int = 512
    max_connections: int = 200
    max_keepalive_connections: int = 100
    structured_mode: str = "json_schema"  # json_schema | guided_json | prompt
    schema_path: Path = DEFAULT_SCHEMA
    strict_keys: bool = True

    def validate(self) -> None:
        if self.slots < max(self.image_slots, self.video_slots):
            raise ValueError("inference.slots must fit the largest request weight")
        if self.workers < 1:
            raise ValueError("inference.workers must be positive")
        if self.structured_mode not in ("json_schema", "guided_json", "prompt"):
            raise ValueError("inference.structured_mode must be json_schema, guided_json or prompt")
        if self.max_retries < 1:
            raise ValueError("inference.max_retries must be at least 1")


@dataclass(slots=True)
class PublishConfig:
    mode: str = "local_jsonl"  # local_jsonl | http
    endpoint: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    batch_min: int = 50
    batch_max: int = 100
    batch_wait_seconds: float = 2.0
    max_retries: int = 3
    timeout_seconds: float = 60.0

    def validate(self) -> None:
        if self.mode not in ("local_jsonl", "http"):
            raise ValueError("publish.mode must be local_jsonl or http")
        if self.mode == "http" and not self.endpoint:
            raise ValueError("publish.endpoint is required when publish.mode is http")
        if not 1 <= self.batch_min <= self.batch_max:
            raise ValueError("publish.batch_min must be within 1..batch_max")
        if self.batch_wait_seconds <= 0:
            raise ValueError("publish.batch_wait_seconds must be positive")


@dataclass(slots=True)
class StateConfig:
    database: Path = Path("data/pipeline/state.sqlite")
    output_dir: Path = Path("outputs/pipeline")


@dataclass(slots=True)
class QueueConfig:
    preprocess: int = 200
    inference: int = 512
    publish: int = 2048
    outstanding_preprocess: int = 24
    metrics_interval_seconds: float = 5.0
    progress: bool = True

    def validate(self) -> None:
        for name in ("preprocess", "inference", "publish", "outstanding_preprocess"):
            if getattr(self, name) < 1:
                raise ValueError(f"queues.{name} must be positive")


@dataclass(slots=True)
class PipelineConfig:
    scan: ScanConfig = field(default_factory=ScanConfig)
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    inference: InferenceConfig = field(default_factory=InferenceConfig)
    publish: PublishConfig = field(default_factory=PublishConfig)
    state: StateConfig = field(default_factory=StateConfig)
    queues: QueueConfig = field(default_factory=QueueConfig)
    job_id: str = ""
    log_level: str = "INFO"

    def validate(self) -> None:
        self.preprocess.validate()
        self.inference.validate()
        self.publish.validate()
        self.queues.validate()
        if not self.scan.input_dirs:
            raise ValueError("at least one scan.input_dirs entry is required")


def _tuple_of_paths(values: Any) -> tuple[Path, ...]:
    if isinstance(values, str):
        return (Path(values),)
    return tuple(Path(item) for item in values)


def _tuple_of_strings(values: Any) -> tuple[str, ...]:
    if isinstance(values, str):
        return (values,)
    return tuple(str(item) for item in values)


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"configuration section {name!r} must be a table")
    return value


def load_config(path: Path, *, job_id: str) -> PipelineConfig:
    """Load one TOML file; unknown keys are rejected so typos never silently change behaviour."""

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    known = {"scan", "preprocess", "inference", "publish", "state", "queues", "job_id", "log_level"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"unknown pipeline configuration keys: {sorted(unknown)}")

    scan = _section(raw, "scan")
    pre = _section(raw, "preprocess")
    infer = _section(raw, "inference")
    publish = _section(raw, "publish")
    state = _section(raw, "state")
    queues = _section(raw, "queues")

    def pick(section: dict[str, Any], name: str, default: Any) -> Any:
        return section.get(name, default)

    config = PipelineConfig(
        scan=ScanConfig(
            input_dirs=tuple(Path(item).expanduser() for item in pick(scan, "input_dirs", ())),
            recursive=bool(pick(scan, "recursive", True)),
            max_file_bytes=int(pick(scan, "max_file_bytes", 2 * 1024**3)),
            skip_hidden=bool(pick(scan, "skip_hidden", True)),
            follow_symlinks=bool(pick(scan, "follow_symlinks", False)),
            include_suffixes=_tuple_of_strings(pick(scan, "include_suffixes", ())),
        ),
        preprocess=PreprocessConfig(
            workers=int(pick(pre, "workers", 12)),
            image_max_edge=int(pick(pre, "image_max_edge", 896)),
            jpeg_quality=int(pick(pre, "jpeg_quality", 85)),
            max_image_bytes=int(pick(pre, "max_image_bytes", 150 * 1024)),
            video_max_edge=int(pick(pre, "video_max_edge", 1280)),
            video_max_frames=int(pick(pre, "video_max_frames", 9)),
            video_columns=int(pick(pre, "video_columns", 3)),
            video_max_seconds=float(pick(pre, "video_max_seconds", 120.0)),
            video_max_wire_bytes=int(pick(pre, "video_max_wire_bytes", 400 * 1024)),
            video_quality=int(pick(pre, "video_quality", 3)),
            decoder=str(pick(pre, "decoder", "auto")),
            ffmpeg=str(pick(pre, "ffmpeg", "ffmpeg")),
            ffprobe=str(pick(pre, "ffprobe", "ffprobe")),
            ffmpeg_threads=int(pick(pre, "ffmpeg_threads", 0)),
        ),
        inference=InferenceConfig(
            base_url=str(pick(infer, "base_url", "http://127.0.0.1:18080/v1")),
            model=str(pick(infer, "model", "models")),
            api_key_env=str(pick(infer, "api_key_env", "MEDIA_TAG_AUTODL_API_KEY")),
            slots=int(pick(infer, "slots", 128)),
            image_slots=int(pick(infer, "image_slots", 1)),
            video_slots=int(pick(infer, "video_slots", 2)),
            workers=int(pick(infer, "workers", 32)),
            timeout_seconds=float(pick(infer, "timeout_seconds", 60.0)),
            max_retries=int(pick(infer, "max_retries", 3)),
            temperature=float(pick(infer, "temperature", 0.1)),
            max_tokens=int(pick(infer, "max_tokens", 512)),
            max_connections=int(pick(infer, "max_connections", 200)),
            max_keepalive_connections=int(pick(infer, "max_keepalive_connections", 100)),
            structured_mode=str(pick(infer, "structured_mode", "json_schema")),
            schema_path=Path(pick(infer, "schema_path", str(DEFAULT_SCHEMA))).expanduser(),
            strict_keys=bool(pick(infer, "strict_keys", True)),
        ),
        publish=PublishConfig(
            mode=str(pick(publish, "mode", "local_jsonl")),
            endpoint=str(pick(publish, "endpoint", "")),
            headers={str(key): str(value) for key, value in pick(publish, "headers", {}).items()},
            batch_min=int(pick(publish, "batch_min", 50)),
            batch_max=int(pick(publish, "batch_max", 100)),
            batch_wait_seconds=float(pick(publish, "batch_wait_seconds", 2.0)),
            max_retries=int(pick(publish, "max_retries", 3)),
            timeout_seconds=float(pick(publish, "timeout_seconds", 60.0)),
        ),
        state=StateConfig(
            database=Path(pick(state, "database", "data/pipeline/state.sqlite")).expanduser(),
            output_dir=Path(pick(state, "output_dir", "outputs/pipeline")).expanduser(),
        ),
        queues=QueueConfig(
            preprocess=int(pick(queues, "preprocess", 200)),
            inference=int(pick(queues, "inference", 512)),
            publish=int(pick(queues, "publish", 2048)),
            outstanding_preprocess=int(pick(queues, "outstanding_preprocess", 24)),
            metrics_interval_seconds=float(pick(queues, "metrics_interval_seconds", 5.0)),
            progress=bool(pick(queues, "progress", True)),
        ),
        job_id=str(pick(raw, "job_id", job_id)),
        log_level=str(pick(raw, "log_level", "INFO")),
    )
    # Relative paths in the file are resolved against the configuration directory.
    base = path.resolve().parent
    for name in ("database", "output_dir"):
        target = getattr(config.state, name)
        if not target.is_absolute():
            setattr(config.state, name, (base / target).resolve())
    if not config.inference.schema_path.is_absolute():
        config.inference.schema_path = (base / config.inference.schema_path).resolve()
    config.scan.input_dirs = tuple(
        item if item.is_absolute() else (base / item).resolve() for item in config.scan.input_dirs
    )
    config.validate()
    return config
