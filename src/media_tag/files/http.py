"""HTTP byte ranges/conditions over an already authorized, verified open handle."""

import re
from urllib.parse import quote
import unicodedata

import anyio
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .service import FileError

EXTENSIONS = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
              'video/mp4': '.mp4', 'video/x-matroska': '.mkv', 'video/x-msvideo': '.avi',
              'audio/wav': '.wav', 'audio/flac': '.flac', 'audio/mpeg': '.mp3', 'application/ogg': '.ogg'}


def disposition(name, asset_id, mime, inline):
    extension = EXTENSIONS.get(mime, '.bin')
    name = unicodedata.normalize('NFC', name)
    name = ''.join('_' if ord(c) < 32 or ord(c) == 127 or c in '<>:"/\\|?*' or unicodedata.category(c) == 'Cf' else c for c in name)
    name = name.strip(' .')[:120]
    stem = name.rsplit('.', 1)[0] if '.' in name else name
    if not stem or stem.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        stem = asset_id
    safe = stem + extension
    return f'{"inline" if inline else "attachment"}; filename="{asset_id}{extension}"; filename*=UTF-8\'\'{quote(safe, safe="")}'


def tag_matches(header, digest, *, weak):
    if header is None:
        return False
    target = f'"{digest}"'
    if len(header) > 2048:
        return False
    return any(tag.strip() == '*' or (tag.strip().removeprefix('W/') if weak else tag.strip()) == target for tag in header.split(','))


def byte_range(header, size):
    """Support one bytes interval; ignore other range units and multipart ranges."""
    if not header or not header.startswith('bytes=') or ',' in header:
        return None
    if len(header) > 256:
        raise FileError(416, 'range_not_satisfiable')
    match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', header)
    if match is None or not any(match.groups()) or size == 0:
        raise FileError(416, 'range_not_satisfiable')
    left, right = match.groups()
    if left:
        start, end = int(left), min(int(right), size - 1) if right else size - 1
        if start >= size or end < start:
            raise FileError(416, 'range_not_satisfiable')
    else:
        length = int(right)
        if length == 0:
            raise FileError(416, 'range_not_satisfiable')
        start, end = max(0, size - length), size - 1
    return start, end


class ManagedStreamingResponse(StreamingResponse):
    def __init__(self, opened, *args, **kwargs):
        self.opened = opened
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Close even when the client disconnects before the first body iteration.
            with anyio.CancelScope(shield=True):
                await run_in_threadpool(self.opened.close)


def transfer(opened, request, *, inline=False):
    headers = {'ETag': f'"{opened.digest}"', 'Accept-Ranges': 'bytes', 'Content-Type': opened.mime,
               'Cache-Control': 'private, no-store', 'Vary': 'Authorization', 'X-Content-Type-Options': 'nosniff',
               'Content-Disposition': disposition(opened.name, opened.asset_id, opened.mime, inline)}
    try:
        if inline and opened.mime == 'application/octet-stream':
            raise FileError(415, 'inline_format_unsupported')
        if request.headers.get('if-match') is not None and not tag_matches(request.headers['if-match'], opened.digest, weak=False):
            raise FileError(412, 'etag_precondition_failed')
        if tag_matches(request.headers.get('if-none-match'), opened.digest, weak=True):
            opened.close()
            return Response(status_code=304, headers=headers)
        interval = None
        range_header = request.headers.get('range')
        if request.method == 'GET' and range_header and (request.headers.get('if-range') is None or request.headers['if-range'] == headers['ETag']):
            interval = byte_range(range_header, opened.size)
        start, end = interval if interval is not None else (0, opened.size - 1)
        length = max(0, end - start + 1)
        headers['Content-Length'] = str(length)
        if interval:
            headers['Content-Range'] = f'bytes {start}-{end}/{opened.size}'
        if request.method == 'HEAD':
            opened.close()
            return Response(status_code=200, headers=headers)
        def chunks():
            opened.stream.seek(start)
            remaining = length
            while remaining:
                chunk = opened.read(min(65536, remaining))
                if not chunk:
                    raise FileError(503, 'content_truncated')
                remaining -= len(chunk)
                yield chunk
        return ManagedStreamingResponse(opened, chunks(), status_code=206 if interval else 200, headers=headers)
    except FileError as exc:
        opened.close()
        error_headers = {'Cache-Control': 'private, no-store', 'Vary': 'Authorization'}
        if exc.status == 416:
            error_headers.update({'Content-Range': f'bytes */{opened.size}', 'Accept-Ranges': 'bytes'})
        return JSONResponse(exc.payload(), status_code=exc.status, headers=error_headers)
    except BaseException:
        opened.close()
        raise
