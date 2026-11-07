"""Bounded intake validation, independent of the filename or any semantic model."""

from pathlib import Path

from media_tag.workers.probe import FORMAT_WHITELIST, MediaError, ProbeOptions, probe_media

EXTENSIONS = {
    '.png': {'png_pipe'}, '.jpg': {'jpeg_pipe'}, '.jpeg': {'jpeg_pipe'}, '.gif': {'gif'},
    '.webp': {'webp_pipe'}, '.bmp': {'bmp_pipe'}, '.tif': {'tiff_pipe'}, '.tiff': {'tiff_pipe'},
    '.mp4': {'mov'}, '.mov': {'mov'}, '.m4v': {'mov'}, '.mkv': {'matroska'}, '.webm': {'matroska'},
    '.avi': {'avi'}, '.ts': {'mpegts'}, '.mpeg': {'mpeg'}, '.mpg': {'mpeg'},
    '.wav': {'wav'}, '.mp3': {'mp3'}, '.flac': {'flac'}, '.ogg': {'ogg'}, '.aac': {'aac'}, '.m4a': {'mov'},
}


def validate_media(path, tools, *, filename=None, full_decode=False):
    info = probe_media(path, tools, ProbeOptions())
    formats = set(info.raw_metadata.get('format', {}).get('format_name', '').split(','))
    if filename is not None and not formats & EXTENSIONS.get(Path(filename).suffix.lower(), set()):
        raise MediaError('format_mismatch', 'actual format does not match the supported filename type')
    args = ['-v', 'error', '-xerror', '-protocol_whitelist', 'file,pipe', '-format_whitelist', FORMAT_WHITELIST,
            '-i', str(path)]
    if not full_decode:
        args += ['-t', '0.25']
    args += ['-map', '0:v?', '-map', '0:a?', '-f', 'null', '-']
    tools.ffmpeg_run(args, timeout=60.0 if full_decode else 20.0)
    return info.as_dict() | {'intake_decode': 'full' if full_decode else 'bounded', 'semantic_review': False}
