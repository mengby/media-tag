"""Authorized, fingerprinted probe/evidence stages in private per-job directories."""

from datetime import datetime, timezone
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from typing import Annotated, Callable

from PIL import Image, ImageOps
from pydantic import Field, TypeAdapter

from media_tag.catalog import Catalog, Principal
from media_tag.contracts.models import ArtifactRef, ContractModel, Evidence, Identifier
from media_tag.storage import IntegrityError, UnsafePath
from media_tag.storage.filesystem import FilesystemBackend, _linked
from .probe import FORMAT_WHITELIST, PROBE_VERSION, MediaError, ProbeOptions, Toolchain, file_sha256, finite_number, probe_media

EVIDENCE_VERSION = "d02-evidence-v1"


class EvidenceOptions(ContractModel):
    max_frames: Annotated[int, Field(ge=1, le=12)] = 3
    max_packets: Annotated[int, Field(ge=1, le=2400)] = 600
    horizon_seconds: Annotated[float, Field(gt=0, le=120)] = 30.0
    thumbnail_edge: Annotated[int, Field(ge=16, le=2048)] = 640
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 20.0


def _json_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _directory(root: Path, relative: str) -> Path:
    # Only generated paths and validated identifiers reach here. Reject pre-existing links before any write.
    cursor = root
    for part in relative.split("/"):
        if part in ("", ".", ".."):
            raise UnsafePath("invalid job path")
        cursor /= part
        if cursor.exists() or cursor.is_symlink():
            if _linked(cursor) or not cursor.is_dir():
                raise UnsafePath("job directories cannot contain links or files")
        else:
            cursor.mkdir()
            (cursor / "README.md").write_text("Private D02 work artifacts. Not a published media directory.\n", encoding="utf-8")
    return cursor


def _artifact(path: Path, root: Path, *, kind: str) -> dict:
    return {"kind": kind, "ref": path.relative_to(root).as_posix(), "sha256": file_sha256(path), "size_bytes": path.stat().st_size}


def _frame_file(path: Path, root: Path, *, source_sha256: str, requested_index: int | None,
                source_pts: float | None, normalized_pts: float | None, decoded_clock: dict | None = None,
                source_pts_ticks: int | None = None) -> dict:
    try:
        with Image.open(path) as image:
            width, height = image.size
            transparent = image.convert("RGBA").getchannel("A").getextrema()[0] < 255
    except (OSError, ValueError) as exc:
        raise MediaError("artifact_decode_failed", "generated image evidence cannot be decoded") from exc
    return {"artifact": _artifact(path, root, kind="image" if source_pts is None else "frame"),
            "source_sha256": source_sha256, "source_frame_index": requested_index,
            "source_pts_seconds": source_pts, "normalized_seconds": normalized_pts,
            "source_pts_ticks": source_pts_ticks, "decoded_clock": decoded_clock,
            "width": width, "height": height, "sample_has_transparent_pixels": transparent}


def extract_frames(path: Path, info: dict, output: Path, root: Path, tools: Toolchain,
                   options: EvidenceOptions, source_sha256: str) -> dict:
    if info["video_stream_index"] is None:
        return {"status": "not_applicable", "reason": "no_visual_stream", "frames": [], "continuous_observation": False}
    if info["kind"] == "image":
        target = output / "frame-001.png"
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image).convert("RGBA")
                image.thumbnail((options.thumbnail_edge, options.thumbnail_edge))
                image.save(target, format="PNG")
        except (OSError, ValueError) as exc:
            raise MediaError("image_evidence_failed", "image preview could not be decoded or written") from exc
        return {"status": "succeeded", "frames": [_frame_file(target, root, source_sha256=source_sha256,
                requested_index=None, source_pts=None, normalized_pts=None)], "continuous_observation": False,
                "transform": {"image_orientation": "exif_transpose", "format": "PNG", "max_edge": options.thumbnail_edge}}
    if info["start_seconds"] is None:
        raise MediaError("unknown_time_origin", "source timestamp origin is missing; do not assume zero")
    raw = tools.probe_run(path, timeout=options.timeout_seconds, extra=["-select_streams", str(info["video_stream_index"]),
                         "-read_intervals", f"%+#{options.max_packets}", "-show_frames", "-show_entries",
                         "frame=best_effort_timestamp,best_effort_timestamp_time,pts,pts_time,duration_time"])
    indexed = []
    all_frames = raw.get("frames", [])
    for index, frame in enumerate(all_frames):
        pts = finite_number(frame.get("pts_time"))
        if pts is not None and pts - info["start_seconds"] >= -0.000001:
            tick_value = frame.get("pts")
            if tick_value is None:
                continue
            indexed.append((index, pts, max(0.0, pts - info["start_seconds"]), int(tick_value) if tick_value is not None else None))
    if not indexed:
        raise MediaError("missing_frame_timestamps", "no decoded visual frames with a valid source timestamp")
    first = indexed[0][2]
    candidates = [frame for frame in indexed if frame[2] <= first + options.horizon_seconds]
    count = min(options.max_frames, len(candidates))
    positions = sorted({round(i * (len(candidates) - 1) / max(1, count - 1)) for i in range(count)})
    selected = [candidates[position] for position in positions]
    select = "+".join(f"eq(n,{frame[0]})" for frame in selected)
    edge = options.thumbnail_edge
    filters = f"select='{select}',showinfo,scale=w='min({edge},iw)':h='min({edge},ih)':force_original_aspect_ratio=decrease:reset_sar=1"
    _, stderr = tools.ffmpeg_run(["-v", "info", "-xerror", "-copyts", "-start_at_zero", "-protocol_whitelist", "file,pipe",
                                 "-format_whitelist", FORMAT_WHITELIST, "-i", str(path), "-map", f"0:{info['video_stream_index']}",
                                 "-an", "-vf", filters, "-frames:v", str(len(selected)), "-fps_mode", "passthrough",
                                 "-threads:v", "1", "-pix_fmt", "rgba", "-f", "image2", str(output / "frame-%03d.png")],
                                timeout=options.timeout_seconds)
    actual_ticks = [int(value) for value in re.findall(rb"\bn:\s*\d+\s+pts:\s*(-?\d+)\s+pts_time:", stderr)]
    time_base_match = re.search(rb"config in time_base:\s*(\d+)/(\d+)", stderr)
    if len(actual_ticks) != len(selected) or time_base_match is None:
        raise MediaError("frame_count_mismatch", "decoded frame timestamps do not match the requested evidence outputs")
    time_base = Fraction(int(time_base_match[1]), int(time_base_match[2]))
    if time_base <= 0:
        raise MediaError("frame_time_base", "decoded frame time base is invalid")
    frames = []
    for number, ((index, expected, _, source_ticks), decoded_ticks) in enumerate(zip(selected, actual_ticks), 1):
        normalized = float(decoded_ticks * time_base)
        source_pts = normalized + info["start_seconds"]
        if abs(source_pts - expected) > max(float(time_base), 0.000002) or normalized < -0.000001:
            raise MediaError("frame_timestamp_mismatch", "FFmpeg frame PTS disagrees with the indexed source timestamp")
        target = output / f"frame-{number:03}.png"
        if not target.is_file():
            raise MediaError("missing_frame_output", "decoder did not produce every requested evidence file")
        frames.append(_frame_file(target, root, source_sha256=source_sha256, requested_index=index,
                                  source_pts=source_pts, normalized_pts=max(0.0, normalized), source_pts_ticks=source_ticks,
                                  decoded_clock={"pts": decoded_ticks, "time_base": str(time_base)}))
    return {"status": "succeeded", "frames": frames, "continuous_observation": False,
            "coverage_kind": "sparse_decoded_frames", "index_packet_budget": options.max_packets,
            "indexed_frame_count": len(all_frames), "eligible_frame_count": len(candidates),
            "unusable_frame_timestamps": len(all_frames) - len(indexed),
            "sampling_horizon_seconds": options.horizon_seconds, "sampled_normalized_seconds": [f["normalized_seconds"] for f in frames],
            "unobserved_between_samples": True, "full_video_observed": False,
            "transform": {"autorotate": True, "copyts_start_at_zero": True, "format": "PNG", "max_edge": edge, "square_pixel_preview": True}}


class MediaAnalyzer:
    def __init__(self, catalog: Catalog, workspace: Path, tools: Toolchain) -> None:
        self.catalog = catalog
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise UnsafePath("job workspace must already exist")
        self.tools = tools

    def _stage(self, job: Path, stage: str, version: str, key: dict, attempt: int,
               function: Callable[[Path], dict]) -> dict:
        fingerprint = sha256(_json_bytes({"version": version, "tools": self.tools.identity, **key})).hexdigest()
        task_root = _directory(self.workspace, f"{job.relative_to(self.workspace).as_posix()}/runs/{stage}-{fingerprint[:24]}")
        backend = FilesystemBackend(self.workspace)
        cache_rejections = []
        for previous in sorted(task_root.glob("attempt-*/stage.json"), reverse=True):
            try:
                ref = previous.relative_to(self.workspace).as_posix()
                with backend.open_read(ref) as stream:
                    raw = stream.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    cache_rejections.append({"attempt": previous.parent.name, "reason": "manifest_budget"})
                    continue
                with backend.open_read(ref + ".sha256") as stream:
                    checksum = stream.read(65).decode("ascii")
                cached = json.loads(raw)
                if sha256(raw).hexdigest() != checksum or cached["fingerprint"] != fingerprint or cached["status"] not in ("succeeded", "not_applicable"):
                    cache_rejections.append({"attempt": previous.parent.name, "reason": "incomplete_or_mismatched_manifest"})
                    continue
                for artifact in cached["artifacts"]:
                    with backend.open_verified(artifact["ref"], artifact["sha256"], artifact["size_bytes"]):
                        pass
                return {**cached, "cache_hit": True, "cache_rejections": cache_rejections}
            except (ValueError, KeyError, OSError, IntegrityError):
                cache_rejections.append({"attempt": previous.parent.name, "reason": "invalid_cached_artifact_or_manifest"})
                continue
            except Exception as exc:
                # Only backend validation errors invalidate a cached stage; programming errors still surface.
                from media_tag.storage import StorageError
                if not isinstance(exc, StorageError):
                    raise
                cache_rejections.append({"attempt": previous.parent.name, "reason": "unreadable_cached_artifact_or_manifest"})
        relative = f"{task_root.relative_to(self.workspace).as_posix()}/attempt-{attempt}"
        output = self.workspace / relative
        if output.exists() or output.is_symlink():
            raise MediaError("attempt_exists", "attempt directory already exists; retry with a new attempt number")
        try:
            output.mkdir()
        except FileExistsError as exc:
            raise MediaError("attempt_exists", "attempt directory was claimed by another execution") from exc
        (output / "README.md").write_text("Private D02 attempt outputs; never a published preview.\n", encoding="utf-8")
        started = time.monotonic()
        report = {"stage": stage, "version": version, "fingerprint": fingerprint, "input": key,
                  "tools": self.tools.identity, "started_at": datetime.now(timezone.utc).isoformat(),
                  "artifacts": [], "errors": [], "cache_hit": False, "cache_rejections": cache_rejections}
        try:
            data = function(output)
            report["status"] = data.pop("status", "succeeded")
            report["errors"] = data.pop("errors", [])
            report["data"] = data
            report["artifacts"] = [frame["artifact"] for frame in data.get("frames", [])]
        except MediaError as exc:
            report |= {"status": "failed", "data": None, "errors": [{"code": exc.code, "message": str(exc)}]}
        report["elapsed_seconds"] = time.monotonic() - started
        raw = _json_bytes(report)
        (output / "stage.json").write_bytes(raw)
        (output / "stage.json.sha256").write_text(sha256(raw).hexdigest(), encoding="ascii")
        return json.loads(raw)

    def analyze(self, asset_id: str, *, principal: Principal, job_id: str, attempt: int = 1,
                probe_options: ProbeOptions | None = None, evidence_options: EvidenceOptions | None = None) -> dict:
        TypeAdapter(Identifier).validate_python(job_id, strict=True)
        if len(job_id) > 64 or type(attempt) is not int or attempt < 1:
            raise ValueError("job_id must be at most 64 characters and attempt must be positive")
        probe_options = probe_options or ProbeOptions()
        evidence_options = evidence_options or EvidenceOptions()
        asset = self.catalog.get_asset(asset_id, principal=principal)
        if asset.size_bytes > probe_options.max_input_bytes:
            raise MediaError("input_budget", "registered source exceeds the input byte budget")
        # The public D01 interface checks current permissions and verifies the registered content before any reuse.
        with self.catalog.open_content(asset_id, principal=principal) as source:
            job = _directory(self.workspace, f"jobs/{job_id}")
            inputs = _directory(self.workspace, f"jobs/{job_id}/inputs")
            input_store = FilesystemBackend(inputs, writable=True)
            input_key = f"objects/{asset.sha256[:2]}/{asset.sha256}"
            if not (inputs / input_key).exists():
                copied = input_store.ingest(source)
                if (copied.sha256, copied.size_bytes) != (asset.sha256, asset.size_bytes):
                    raise IntegrityError("authorized source changed before staging")
        with input_store.open_verified(input_key, asset.sha256, asset.size_bytes) as stream:
            path = Path(stream.name)
            context = {"asset_id": asset_id, "source_sha256": asset.sha256, "actor_id": principal.actor_id}
            def run_probe(_):
                data = probe_media(path, self.tools, probe_options).as_dict()
                failures = [{"code": track["reason"], "message": "audio scan did not complete"} for track in data["audio_tracks"]
                            if track["reason"] in ("decode_failed", "tool_timeout", "output_budget", "tool_unavailable", "unsupported_format")]
                return data | {"status": "partial" if failures else "succeeded", "errors": failures}
            probe = self._stage(job, "probe", PROBE_VERSION, context | {"options": probe_options.model_dump()}, attempt,
                                run_probe)
            if probe["status"] == "failed":
                return {"asset_id": asset_id, "asset_revision": asset.revision, "status": "failed", "probe": probe,
                        "evidence": {"status": "blocked", "reason": "probe_failed"}}
            info = probe["data"]
            evidence = self._stage(job, "evidence", EVIDENCE_VERSION,
                                   context | {"probe_fingerprint": probe["fingerprint"], "options": evidence_options.model_dump()}, attempt,
                                   lambda output: extract_frames(path, info, output, self.workspace, self.tools, evidence_options, asset.sha256))
        return {"asset_id": asset_id, "asset_revision": asset.revision, "status": "partial" if evidence["status"] == "failed" or probe["status"] == "partial" else "succeeded",
                "probe": probe, "evidence": evidence}


def evidence_records(report: dict) -> tuple[Evidence, ...]:
    stage = report["evidence"]
    if stage["status"] != "succeeded":
        return ()
    records = []
    for index, frame in enumerate(stage["data"]["frames"]):
        artifact = frame["artifact"]
        records.append(Evidence(evidence_id=f"evidence-{stage['fingerprint'][:24]}-{index}",
                                kind=artifact["kind"], source_sha256=frame["source_sha256"],
                                artifact=ArtifactRef(kind=artifact["kind"], ref=artifact["ref"], sha256=artifact["sha256"]),
                                start_seconds=frame["normalized_seconds"]))
    return tuple(records)
