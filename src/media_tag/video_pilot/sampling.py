"""Prepare sparse reference frames or shot frames with short continuous action clips."""

import json
import math
from pathlib import Path
import re
import statistics
from io import BytesIO

from PIL import Image, ImageChops, ImageDraw, ImageStat

from media_tag.workers.evidence import EvidenceOptions, _duration_samples
from media_tag.workers.probe import FORMAT_WHITELIST, Toolchain, file_sha256

MIN_SHOT = 0.55
MAX_SHOTS = 4
MAX_CLIPS = 5
CLIP_SECONDS = 3.0
MOTION_FPS = 4
SCALE = "scale=w='min(640,iw)':h='min(640,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2:reset_sar=1"


def source_info(path, tools):
    if not path.is_file():
        raise ValueError('source file is missing')
    info = tools.probe_run(path, timeout=30)
    streams = [s for s in info['streams']
               if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise ValueError('no visual stream')
    stream = streams[0]
    duration = float(stream.get('duration') or info['format'].get('duration') or 0)
    if not 0 < duration <= 120 or stream['width'] * stream['height'] > 4096 * 2160:
        raise ValueError('pilot supports visual streams up to 120 seconds and 4K')
    return dict(duration_seconds=duration, start_seconds=float(stream.get('start_time') or 0),
                video_stream_index=stream['index'], portrait=stream['height'] > stream['width'])


def _input(path, start=None):
    seek = ['-ss', f'{start:.6f}'] if start is not None else []
    return [*seek, '-protocol_whitelist', 'file,pipe', '-format_whitelist', FORMAT_WHITELIST, '-i', str(path)]


def scene_motion(tools, path, info):
    # Score cuts on decoded frames. Only the low-resolution motion trace uses a fixed sampling rate.
    vf = ('setpts=PTS-STARTPTS,scale=96:64,scdet=threshold=10,'
          f'metadata=print:key=lavfi.scd.time,fps={MOTION_FPS},format=gray')
    raw, log = tools.ffmpeg_run(
        ['-v', 'info', *_input(path), '-map', f"0:{info['video_stream_index']}",
         '-an', '-vf', vf, '-f', 'rawvideo', 'pipe:1'],
        timeout=120, stdout_limit=4 * 1024 * 1024,
    )
    duration = info['duration_seconds']
    cuts = [0.0]
    for value in re.findall(rb'lavfi\.scd\.time=([-+.\d]+)', log):
        t = float(value)
        if t - cuts[-1] >= MIN_SHOT and duration - t >= MIN_SHOT:
            cuts.append(t)
    cuts.append(duration)
    size = 96 * 64
    if len(raw) % size:
        raise ValueError('incomplete motion trace')
    motion = []
    previous = None
    for off in range(0, len(raw), size):
        frame = Image.frombytes('L', (96, 64), raw[off:off+size])
        if previous is not None:
            difference = ImageStat.Stat(ImageChops.difference(previous, frame)).mean[0]
            motion.append((off / size / MOTION_FPS, difference))
        previous = frame
    return list(zip(cuts, cuts[1:])), motion


def plan_windows(shots, motion):
    if len(shots) <= MAX_SHOTS:
        selected = list(range(len(shots)))
    else:
        selected = [round(i * (len(shots)-1) / (MAX_SHOTS-1)) for i in range(MAX_SHOTS)]
    counts = {i: 1 for i in selected}
    while sum(counts.values()) < MAX_CLIPS:
        idx = max(selected, key=lambda i: (shots[i][1] - shots[i][0]) / counts[i])
        if (shots[idx][1] - shots[idx][0]) / counts[idx] < 8:
            break
        counts[idx] += 1
    windows = []
    for idx in selected:
        left, right = shots[idx]
        n = counts[idx]
        for j in range(n):
            a = left + (right - left) * j / n
            b = left + (right - left) * (j+1) / n
            width = min(CLIP_SECONDS, b-a)
            center = (a + b - width) / 2
            candidates = {a, center, b-width}
            candidates.update(k / MOTION_FPS for k in range(
                math.ceil(a * MOTION_FPS), math.floor((b-width) * MOTION_FPS) + 1))

            def score(t):
                values = [v for at, v in motion if t + 1/MOTION_FPS <= at < t + width]
                return (statistics.fmean(values) if values else 0, -abs(t - center))

            start = max(sorted(candidates), key=score)
            windows.append((idx, start, min(b, start+width)))
    return selected, windows


def _entry(folder, path, ident, kind, start, end, shot=None):
    return dict(id=ident, kind=kind, start=round(start, 6), end=round(end, 6), shot=shot,
                path=path.relative_to(folder).as_posix(), sha256=file_sha256(path), bytes=path.stat().st_size)


def temporal_sheet(tools, path, info, folder, ident, shot, start, end):
    """Decode one uninterrupted interval locally, retaining each selected frame's clock."""
    width, height = (224, 288) if info['portrait'] else (320, 224)
    frame_size = width * height * 3
    filters = (
        f"trim=duration={end - start:.6f},"
        "select='isnan(prev_selected_t)+gte(t-prev_selected_t,0.24)',"
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:reset_sar=1,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,format=rgb24,showinfo"
    )
    raw, log = tools.ffmpeg_run(
        ['-v', 'info', *_input(path, start), '-map', f"0:{info['video_stream_index']}", '-an',
         '-vf', filters, '-fps_mode', 'passthrough', '-f', 'rawvideo', 'pipe:1'],
        timeout=60, stdout_limit=frame_size * 15,
    )
    clock_pattern = rb'\bn:\s*\d+\s+pts:\s*-?\d+\s+pts_time:([-+\d.eE]+)'
    times = [start + float(pts) for pts in re.findall(clock_pattern, log)]
    if len(raw) != len(times) * frame_size or any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError('invalid decoded sequence clock')
    if len(times) < 2:
        return None
    columns = min(4 if info['portrait'] else 3, len(times))
    cell_height = height + 20
    sheet = Image.new('RGB', (columns * width, math.ceil(len(times) / columns) * cell_height))
    draw = ImageDraw.Draw(sheet)
    for index, timestamp in enumerate(times):
        begin = index * frame_size
        frame = Image.frombytes('RGB', (width, height), raw[begin:begin + frame_size])
        x, y = index % columns * width, index // columns * cell_height
        sheet.paste(frame, (x, y))
        draw.text((x + 3, y + height + 2), f'{ident}  {timestamp:.2f} s', fill='white')
    target = folder / f'{ident}.jpg'
    sheet.save(target, quality=88)
    return _entry(folder, target, ident, 'sequence', start, end, shot) | {
        'frame_seconds': [round(t, 6) for t in times], 'columns': columns,
    }


def prepare(path: Path, folder: Path, method: str, tools: Toolchain):
    path = path.resolve(strict=True)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / 'README.md').write_text('Local visual tagging evidence and model reply for one source.\n',
                                      encoding='utf-8')
    info = source_info(path, tools)
    source_sha = file_sha256(path)
    entries = []
    if method == 'three':
        report = _duration_samples(path, info, folder, folder, tools, EvidenceOptions(), source_sha)
        for idx, frame in enumerate(report['frames'], 1):
            image = folder / frame['artifact']['ref']
            jpg = folder / f'F{idx}.jpg'
            with Image.open(image) as pic:
                pic.convert('RGB').save(jpg, quality=87)
            t = frame['normalized_seconds']
            entries.append(_entry(folder, jpg, f'F{idx}', 'image', t, t))
        shots, selected = [], []
    elif method == 'shots':
        shots, motion = scene_motion(tools, path, info)
        selected, windows = plan_windows(shots, motion)
        stream = ['-map', f"0:{info['video_stream_index']}", '-an', '-sn', '-dn']
        for i, idx in enumerate(selected, 1):
            t = sum(shots[idx]) / 2
            jpg = folder / f'F{i}.jpg'
            raw, _ = tools.ffmpeg_run(
                ['-v', 'error', *_input(path, t), *stream, '-vf', SCALE, '-frames:v', '1',
                 '-c:v', 'png', '-f', 'image2pipe', 'pipe:1'], timeout=45,
            )
            with Image.open(BytesIO(raw)) as pic:
                pic.convert('RGB').save(jpg, quality=87)
            entries.append(_entry(folder, jpg, f'F{i}', 'image', t, t, idx+1))
        for i, (idx, a, b) in enumerate(windows, 1):
            sequence = temporal_sheet(tools, path, info, folder, f'C{i}', idx+1, a, b)
            if sequence:
                entries.append(sequence)
    else:
        raise ValueError('unknown sampling method')
    if file_sha256(path) != source_sha:
        raise ValueError('source changed during sampling')
    manifest = dict(method=method, source_sha256=source_sha, duration=info['duration_seconds'],
                    shots=shots, selected_shots=[i+1 for i in selected], evidence=entries)
    (folder / 'samples.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    return manifest
