"""Bounded intake validation, independent of the filename or any semantic model."""

from pathlib import Path

from media_tag.analysis.probe import semantic_content_kind
from media_tag.workers.probe import FORMAT_WHITELIST, MediaError, ProbeOptions, probe_media

EXTENSIONS = {
    '.png': {'png_pipe'}, '.jpg': {'jpeg_pipe'}, '.jpeg': {'jpeg_pipe'}, '.jfif': {'jpeg_pipe'}, '.gif': {'gif'},
    '.webp': {'webp_pipe'}, '.bmp': {'bmp_pipe'}, '.tif': {'tiff_pipe'}, '.tiff': {'tiff_pipe'},
    '.mp4': {'mov'}, '.mov': {'mov'}, '.m4v': {'mov'}, '.mkv': {'matroska'}, '.webm': {'matroska'},
    '.avi': {'avi'}, '.ts': {'mpegts'}, '.mpeg': {'mpeg'}, '.mpg': {'mpeg'},
    '.wav': {'wav'}, '.mp3': {'mp3'}, '.flac': {'flac'}, '.ogg': {'ogg'}, '.aac': {'aac'}, '.m4a': {'mov'},
}


def _ebml_value(data, pos, *, size):
    """Read one EBML element ID or size, including non-minimal size encodings."""
    if pos >= len(data) or not data[pos]:
        raise ValueError('truncated EBML integer')
    width = 9 - data[pos].bit_length()
    end = pos + width
    if end > len(data) or (not size and width > 4):
        raise ValueError('invalid EBML integer')
    value = int.from_bytes(data[pos:end], 'big')
    return (value ^ (1 << (7 * width)) if size else value), end


def _webm_header(data):
    if not data.startswith(b'\x1a\x45\xdf\xa3'):
        return False
    try:
        length, pos = _ebml_value(data, 4, size=True)
        end = min(len(data), pos + length)
        while pos < end:
            element, pos = _ebml_value(data, pos, size=False)
            length, pos = _ebml_value(data, pos, size=True)
            if pos + length > end:
                return False
            if element == 0x4282:  # DocType in the EBML header
                return data[pos:pos + length] == b'webm'
            pos += length
    except ValueError:
        pass
    return False


def upload_name_parts(validation, header):
    kind = semantic_content_kind(validation)
    label = {'image': '图片', 'animation': '动画', 'video': '视频', 'audio': '音频'}[kind]
    container = validation['raw_metadata']['format']
    formats = set(container['format_name'].split(','))
    # ffprobe groups QuickTime/MP4 and Matroska/WebM under shared demuxer names.
    if 'mov' in formats:
        if container.get('tags', {}).get('major_brand', 'qt  ') == 'qt  ':
            return label, '.mov'
        return label, '.m4a' if kind == 'audio' else '.mp4'
    if 'matroska' in formats:
        return label, '.webm' if _webm_header(header) else '.mkv'
    # The first extension for each validated demuxer is its canonical spelling.
    return label, next(ext for ext, accepted in EXTENSIONS.items() if formats & accepted)


def validate_media(path, tools, *, filename=None, full_decode=False, probe_options=None):
    info = probe_media(path, tools, probe_options or ProbeOptions())
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
