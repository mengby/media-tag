"""Bounded deterministic media probing. No labels, permissions or source paths are guessed."""

from array import array
from dataclasses import asdict, dataclass
from hashlib import sha256
from importlib.resources import files
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Annotated
import warnings

from PIL import Image, UnidentifiedImageError, __version__ as pillow_version
from pydantic import Field

from media_tag.contracts.models import ContractModel

PROBE_VERSION = "d02-probe-v1"
# No playlist, concat, device, network or script demuxers. MOV external data references remain disabled.
FORMAT_WHITELIST = "mov,matroska,webm,avi,mpegts,mpeg,mp3,wav,flac,ogg,aac,gif,png_pipe,jpeg_pipe,bmp_pipe,tiff_pipe,webp_pipe,image2"


class MediaError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def finite_number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def run_bounded(program: Path, args: list[str], *, timeout: float, stdout_limit: int = 8 * 1024 * 1024,
                stderr_limit: int = 1024 * 1024) -> tuple[bytes, bytes]:
    """Drain both pipes concurrently, kill on timeout/output cap, and never echo tool logs."""
    try:
        process = subprocess.Popen([str(program), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, shell=False,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except OSError as exc:
        raise MediaError("tool_unavailable", "media executable cannot be started") from exc
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    def drain(pipe, index, limit):
        try:
            while chunk := pipe.read(65536):
                remaining = max(0, limit - len(buffers[index]))
                buffers[index].extend(chunk[:remaining])
                if len(chunk) > remaining:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
        finally:
            pipe.close()
    threads = [threading.Thread(target=drain, args=(process.stdout, 0, stdout_limit), daemon=True),
               threading.Thread(target=drain, args=(process.stderr, 1, stderr_limit), daemon=True)]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        process.wait()
    except BaseException:
        # A local executor may release its quota only after all launched tools have stopped.
        process.kill()
        process.wait()
        raise
    finally:
        for thread in threads:
            thread.join()
    if timed_out:
        raise MediaError("tool_timeout", "media process exceeded its time budget")
    if overflow.is_set():
        raise MediaError("output_budget", "media process exceeded its output budget")
    if process.returncode:
        code = "unsupported_format" if b"whitelist" in buffers[1].lower() else "decode_failed"
        raise MediaError(code, "media command failed; input may be unsupported or damaged")
    return bytes(buffers[0]), bytes(buffers[1])


class Toolchain:
    def __init__(self, directory: Path | None = None) -> None:
        explicit = directory is not None or bool(os.environ.get("MEDIA_TAG_TOOL_DIR"))
        directory = Path(directory or os.environ.get("MEDIA_TAG_TOOL_DIR", "cache/media-tools/ffmpeg-9.0.2/bin"))
        self.ffmpeg = (directory / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")).resolve()
        self.ffprobe = (directory / ("ffprobe.exe" if os.name == "nt" else "ffprobe")).resolve()
        self.identity = {"pillow": pillow_version}
        lock = json.loads(files("media_tag.workers").joinpath("media-tools.lock.json").read_text(encoding="utf-8"))
        for name in ("ffmpeg", "ffprobe"):
            path = getattr(self, name)
            if not path.is_file():
                raise MediaError("tool_missing", "FFmpeg/ffprobe pair is missing; configure MEDIA_TAG_TOOL_DIR")
            digest = file_sha256(path)
            if not explicit and (os.name != "nt" or digest != lock[f"{name}_sha256"]):
                raise MediaError("tool_hash_mismatch", "default media tool does not match the pinned Windows build")
            output, _ = run_bounded(path, ["-version"], timeout=10)
            first = output.decode("utf-8", errors="replace").splitlines()[0]
            if not first.startswith(f"{name} version "):
                raise MediaError("tool_identity", "executable did not identify as the requested media tool")
            self.identity[name] = {"version": first, "sha256": digest}

    def ffmpeg_run(self, args: list[str], *, timeout: float, stdout_limit: int = 8 * 1024 * 1024) -> tuple[bytes, bytes]:
        return run_bounded(self.ffmpeg, ["-nostdin", "-hide_banner", "-threads", "1", "-filter_threads", "1",
                                         "-filter_complex_threads", "1", *args], timeout=timeout, stdout_limit=stdout_limit)

    def probe_run(self, path: Path, *, timeout: float, extra: list[str] | None = None) -> dict:
        args = ["-v", "error", "-threads", "1", "-protocol_whitelist", "file,pipe", "-format_whitelist", FORMAT_WHITELIST,
                *(extra or ["-show_entries", "format=format_name,duration,start_time:stream=index,codec_name,codec_type,width,height,pix_fmt,avg_frame_rate,r_frame_rate,time_base,start_time,duration,sample_rate,channels,nb_frames,sample_aspect_ratio:stream_disposition=attached_pic:stream_tags=rotate:stream_side_data=rotation"]),
                "-of", "json", str(path)]
        output, _ = run_bounded(self.ffprobe, args, timeout=timeout)
        try:
            result = json.loads(output)
            if not isinstance(result, dict):
                raise ValueError("not an object")
            return result
        except (ValueError, UnicodeError) as exc:
            raise MediaError("probe_output", "ffprobe did not return a JSON object") from exc


class ProbeOptions(ContractModel):
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = 20.0
    max_input_bytes: Annotated[int, Field(ge=1)] = 2 * 1024 ** 3
    max_pixels: Annotated[int, Field(ge=1, le=64 * 1024 ** 2)] = 16 * 1024 ** 2
    max_alpha_frames: Annotated[int, Field(ge=1, le=512)] = 32
    max_image_bytes: Annotated[int, Field(ge=1, le=256 * 1024 ** 2)] = 64 * 1024 ** 2
    max_alpha_pixels: Annotated[int, Field(ge=1, le=256 * 1024 ** 2)] = 32 * 1024 ** 2
    audio_scan_seconds: Annotated[float, Field(gt=0, le=120)] = 20.0
    audio_output_bytes: Annotated[int, Field(ge=1024, le=128 * 1024 ** 2)] = 64 * 1024 ** 2
    silence_db: Annotated[float, Field(ge=-120, le=0)] = -60.0


@dataclass(frozen=True)
class ProbeResult:
    kind: str
    width: int | None
    height: int | None
    display_width: int | None
    display_height: int | None
    rotation_degrees: float | None
    duration_seconds: float | None
    start_seconds: float | None
    video_stream_index: int | None
    has_audio: bool
    transparency: dict
    audio_tracks: tuple[dict, ...]
    raw_metadata: dict
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return asdict(self)


def _image_info(path: Path, options: ProbeOptions) -> dict | None:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                if image.format not in {"PNG", "JPEG", "GIF", "WEBP", "BMP", "TIFF"}:
                    raise MediaError("unsupported_image", "image format is outside the D02 supported set")
                if image.width * image.height > options.max_pixels:
                    raise MediaError("pixel_budget", "image exceeds the decoded pixel budget")
                if path.stat().st_size > options.max_image_bytes:
                    raise MediaError("image_byte_budget", "image exceeds the compressed byte budget")
                image_format = image.format
                width, height = image.size
                orientation = image.getexif().get(274)
                orientation = orientation if type(orientation) is int and 1 <= orientation <= 8 else None
                try:
                    image.seek(1)
                    animated = True
                except EOFError:
                    animated = False
                image.seek(0)
                checked, pixels, frame_count = 0, 0, 1 if not animated else None
                transparent = False
                reason = "alpha_frame_budget"
                for index in range(options.max_alpha_frames):
                    try:
                        image.seek(index)
                    except EOFError:
                        frame_count = index
                        break
                    if image.width * image.height > options.max_pixels:
                        raise MediaError("pixel_budget", "animation frame exceeds the pixel budget")
                    if pixels + image.width * image.height > options.max_alpha_pixels:
                        reason = "alpha_pixel_budget"
                        break
                    transparent |= image.convert("RGBA").getchannel("A").getextrema()[0] < 255
                    checked += 1
                    pixels += image.width * image.height
                if frame_count is None:
                    try:
                        image.seek(checked)
                    except EOFError:
                        frame_count = checked
                complete = frame_count is not None and checked == frame_count
                display_width, display_height = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
                return {"kind": "gif" if image_format == "GIF" else "animation" if animated else "image",
                        "format": image_format, "exif_orientation": orientation,
                        "width": width, "height": height, "display_width": display_width,
                        "display_height": display_height, "frames": frame_count,
                        "transparency": {"has_transparent_pixels": True if transparent else False if complete else None,
                                         "checked_frames": checked, "total_frames": frame_count,
                                         "complete": complete,
                                         "reason": None if complete or transparent else reason}}
    except UnidentifiedImageError:
        return None
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise MediaError("pixel_budget", "image decompression exceeds the safety budget") from exc
    except (OSError, EOFError, ValueError) as exc:
        raise MediaError("image_decode_failed", "image cannot be completely decoded within the selected scope") from exc


def _audio_track(tools: Toolchain, path: Path, stream: dict, options: ProbeOptions) -> dict:
    index = int(stream["index"])
    sample_rate = int(stream.get("sample_rate", 0))
    channels = int(stream.get("channels", 0))
    duration = finite_number(stream.get("duration"))
    result = {"stream_index": index, "sample_rate": sample_rate, "channels": channels,
              "duration_seconds": duration, "start_seconds": finite_number(stream.get("start_time")),
              "silent": None, "sampled_silent": None, "complete": False, "scanned_seconds": 0.0,
              "threshold_db": options.silence_db, "reason": None}
    if sample_rate <= 0 or channels <= 0 or channels > 8:
        return result | {"reason": "unknown_or_unsupported_audio_layout"}
    seconds = min(options.audio_scan_seconds, options.audio_output_bytes / (4 * channels * sample_rate))
    try:
        output, _ = tools.ffmpeg_run(["-v", "error", "-xerror", "-protocol_whitelist", "file,pipe",
                                     "-format_whitelist", FORMAT_WHITELIST, "-i", str(path), "-map", f"0:{index}",
                                     "-t", str(seconds), "-vn", "-threads:a", "1", "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"],
                                    timeout=options.timeout_seconds, stdout_limit=options.audio_output_bytes)
    except MediaError as exc:
        return result | {"reason": exc.code}
    if not output or len(output) % (4 * channels):
        return result | {"reason": "no_complete_audio_samples"}
    samples = array("f")
    samples.frombytes(output)
    if sys.byteorder != "little":
        samples.byteswap()
    if any(not math.isfinite(value) for value in samples):
        return result | {"reason": "nonfinite_audio_samples"}
    peak = max(abs(value) for value in samples)
    scanned = len(samples) / channels / sample_rate
    sampled_silent = peak <= 10 ** (options.silence_db / 20)
    complete = duration is not None and duration <= seconds + 1 / sample_rate and scanned >= duration - max(0.001, 2 / sample_rate)
    return result | {"silent": sampled_silent if complete or not sampled_silent else None,
                     "sampled_silent": sampled_silent, "complete": complete, "scanned_seconds": scanned,
                     "peak_amplitude": peak, "reason": None if complete or not sampled_silent else "audio_scan_budget_or_unknown_duration"}


def probe_media(path: Path, tools: Toolchain, options: ProbeOptions) -> ProbeResult:
    image = _image_info(path, options)
    raw = tools.probe_run(path, timeout=options.timeout_seconds)
    for stream in raw.get("streams", []):
        if "tags" in stream:
            # Keep numeric rotation only; never retain arbitrary embedded metadata or signed links.
            rotate = finite_number(stream["tags"].get("rotate"))
            stream["tags"] = {"rotate": rotate} if rotate is not None else {}
    if image:
        raw["image_metadata"] = {"format": image["format"], "frame_count": image["frames"], "exif_orientation": image["exif_orientation"]}
    streams = raw.get("streams", [])
    visual = [stream for stream in streams if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic")]
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not image and not visual and not audio:
        raise MediaError("unsupported_media", "no supported visual or audio stream found")
    video = visual[0] if visual else None
    width, height = (image["width"], image["height"]) if image else (video.get("width"), video.get("height")) if video else (None, None)
    if width and height and width * height > options.max_pixels:
        raise MediaError("pixel_budget", "video dimensions exceed the decode budget")
    rotation = None
    if video:
        rotations = [finite_number(item.get("rotation")) for item in video.get("side_data_list", []) if "rotation" in item]
        rotation = next((value for value in rotations if value is not None), finite_number(video.get("tags", {}).get("rotate")))
    display_width, display_height = (image["display_width"], image["display_height"]) if image else (width, height)
    if not image and rotation is not None and abs(rotation) % 180 == 90:
        display_width, display_height = height, width
    duration = finite_number(raw.get("format", {}).get("duration"))
    if image and image["frames"] == 1:
        duration = None  # A synthetic FFmpeg image time base is not a still-image duration.
    kind = image["kind"] if image else "video" if video else "audio"
    transparency = image["transparency"] if image else {"has_transparent_pixels": None, "complete": False,
                                                        "reason": "video_pixels_not_fully_scanned" if video else "not_applicable"}
    tracks = tuple(_audio_track(tools, path, stream, options) for stream in audio)
    flags = []
    if len(visual) > 1:
        flags.append("multiple_visual_streams_only_first_sampled")
    if image and not image["transparency"]["complete"]:
        flags.append("alpha_coverage_limited")
    flags.extend(f"audio_{track['stream_index']}:{track['reason']}" for track in tracks if track["reason"])
    return ProbeResult(kind, width, height, display_width, display_height, rotation, duration,
                       finite_number(raw.get("format", {}).get("start_time")),
                       int(video["index"]) if video else None, bool(audio), transparency, tracks, raw, tuple(flags))
