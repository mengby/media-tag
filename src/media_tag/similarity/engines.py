"""Bounded actual-media fingerprints. Similarity is a candidate, never a duplicate verdict."""
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
import math
import json
from pathlib import Path
import warnings

from PIL import Image, ImageOps, ImageStat, __version__ as PIL_VERSION

from media_tag.workers.evidence import EvidenceOptions, extract_frames
from media_tag.workers.probe import FORMAT_WHITELIST, MediaError, Toolchain
from media_tag.storage.filesystem import require_local_path

from .contracts import AUDIO_VERSION, CLIP_VERSION, VISUAL_VERSION, AudioFingerprint, Candidate, ClipFingerprint, SourceVersion, VisualFingerprint, VisualFrame


class SimilarityError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def verified_bytes(path: Path, expected_sha256: str, *, max_bytes=128*1024**2):
    if not 1 <= max_bytes <= 512*1024**2:
        raise SimilarityError('source_byte_budget')
    with Path(path).open('rb') as stream:
        data = stream.read(max_bytes+1)
    if len(data) > max_bytes:
        raise SimilarityError('source_byte_budget')
    if sha256(data).hexdigest() != expected_sha256:
        raise SimilarityError('source_integrity')
    return data


def _frame(image, *, seconds=None):
    rgba = image.convert('RGBA')
    alpha = rgba.getchannel('A')
    # Hidden RGB behind transparent pixels must not dominate the visible fingerprint.
    matte = Image.new('RGBA', rgba.size, (128, 128, 128, 255))
    rgb = Image.alpha_composite(matte, rgba).convert('RGB')
    gray = rgb.convert('L').resize((17, 16), Image.Resampling.LANCZOS)
    pixels = list(gray.get_flattened_data())
    bits = 0
    for y in range(16):
        for x in range(16):
            bits = (bits << 1) | int(pixels[y*17+x] > pixels[y*17+x+1])
    alpha_bits = 0
    for value in alpha.resize((16, 16), Image.Resampling.LANCZOS).get_flattened_data():
        alpha_bits = (alpha_bits << 1) | int(value >= 128)
    return VisualFrame(dhash=f'{bits:064x}', alpha_hash=f'{alpha_bits:064x}',
        mean_rgb=tuple(float(x) for x in ImageStat.Stat(rgb).mean),
        luma_grid=tuple(float(x) for x in rgb.convert('L').resize((8, 8), Image.Resampling.LANCZOS).get_flattened_data()),
        normalized_seconds=seconds)


def visual_bytes(data: bytes, source: SourceVersion, *, max_pixels=16*1024**2, max_frames=96, max_total_pixels=64*1024**2):
    if len(data) > 128*1024**2 or not 1 <= max_frames <= 96 or not 1 <= max_pixels <= 16*1024**2 or not 1 <= max_total_pixels <= 64*1024**2:
        raise SimilarityError('visual_budget')
    if sha256(data).hexdigest() != source.content_sha256:
        raise SimilarityError('source_integrity')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                count = getattr(image, 'n_frames', 1)
                if count > max_frames:
                    raise SimilarityError('animation_frame_budget')
                frames, durations, total, clock = [], [], 0, 0.0
                for index in range(count):
                    image.seek(index)
                    total += image.width*image.height
                    if image.width*image.height > max_pixels or total > max_total_pixels:
                        raise SimilarityError('visual_pixel_budget')
                    # Some decoders (notably WebP) populate timing only after load.
                    image.load()
                    duration = image.info.get('duration')
                    duration = float(duration) if type(duration) in (int, float, Fraction) and math.isfinite(duration) and duration >= 0 else None
                    durations.append(duration)
                    frames.append(_frame(ImageOps.exif_transpose(image.copy()), seconds=clock if count > 1 else None))
                    if duration is None:
                        clock = None
                    elif clock is not None:
                        clock += duration/1000.0
                kind = 'image' if count == 1 else 'animation'
                return VisualFingerprint(source=source, media_kind=kind, frames=tuple(frames),
                    frame_durations_ms=() if count == 1 else tuple(durations),
                    coverage='complete_image' if count == 1 else 'complete_animation_frames',
                    decoder_identity='Pillow-'+PIL_VERSION)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError, EOFError) as exc:
        raise SimilarityError('visual_decode_failed') from exc


def _input(data, source, workdir):
    folder = Path(workdir)
    if not folder.is_absolute():
        raise SimilarityError('absolute_private_work_directory_required')
    require_local_path(folder)
    if len(data) > 128*1024**2:
        raise SimilarityError('source_byte_budget')
    folder.mkdir(exist_ok=False)
    (folder/'README.md').write_text('Private similarity work products from authorized synthetic/user-selected input. No approvals.\n', encoding='utf8')
    if sha256(data).hexdigest() != source.content_sha256:
        raise SimilarityError('source_integrity')
    target = folder/'input.bin'
    target.write_bytes(data)
    return folder, target


def _span(stream):
    try:
        value = float(stream['duration']) if stream.get('duration') is not None else float(int(stream['duration_ts'])*Fraction(stream['time_base']))
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def video_bytes(data: bytes, source: SourceVersion, workdir: Path, tools: Toolchain, *, max_frames=8, timeout=20.0):
    folder, path = _input(data, source, workdir)
    raw = tools.probe_run(path, timeout=timeout)
    streams = [s for s in raw.get('streams', []) if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if len(streams) != 1:
        raise SimilarityError('visual_track_selection_required')
    stream = streams[0]
    try:
        origin = float(stream['start_time'])
    except (KeyError, TypeError, ValueError):
        raise SimilarityError('visual_origin_required') from None
    if not math.isfinite(origin):
        raise SimilarityError('visual_origin_required')
    if int(stream.get('width', 0))*int(stream.get('height', 0)) > 16*1024**2:
        raise SimilarityError('visual_pixel_budget')
    info = {'kind': 'video', 'video_stream_index': int(stream['index']), 'start_seconds': origin}
    evidence = extract_frames(path, info, folder, folder, tools,
        EvidenceOptions(max_frames=max_frames, thumbnail_edge=320, timeout_seconds=timeout), source.content_sha256)
    frames = []
    for row in evidence['frames']:
        ref = row['artifact']
        frame_bytes = verified_bytes(folder/ref['ref'], ref['sha256'], max_bytes=8*1024**2)
        with Image.open(BytesIO(frame_bytes)) as image:
            frames.append(_frame(image, seconds=float(row['normalized_seconds'])))
    if sha256(path.read_bytes()).hexdigest() != source.content_sha256:
        raise SimilarityError('source_integrity')
    return VisualFingerprint(source=source, media_kind='video', frames=tuple(frames),
        coverage='sparse_decoded_video_frames', source_origin_seconds=origin,
        decoder_identity='FFmpeg-'+sha256(json.dumps(tools.identity,sort_keys=True).encode()).hexdigest())


def audio_bytes(data: bytes, source: SourceVersion, workdir: Path, tools: Toolchain, *, stream_index=None, max_seconds=120.0, timeout=30.0):
    if not 0 < max_seconds <= 120.0:
        raise SimilarityError('audio_duration_budget')
    try:
        import numpy as np
    except ImportError:
        raise SimilarityError('audio_numpy_extra_unavailable') from None
    folder, path = _input(data, source, workdir)
    raw = tools.probe_run(path, timeout=timeout)
    streams = [s for s in raw.get('streams', []) if s.get('codec_type') == 'audio']
    selected = [s for s in streams if int(s['index']) == stream_index] if stream_index is not None else streams
    if len(selected) != 1:
        raise SimilarityError('audio_track_selection_required')
    stream = selected[0]
    span = _span(stream)
    if span is not None and span > max_seconds+.001:
        raise SimilarityError('audio_duration_budget')
    maximum = int((max_seconds+.5)*16000)*2
    try:
        # Normalize selected-stream PTS, then decode to EOF. An output -t would
        # truncate delayed/gapped streams and could leave an apparently short
        # "complete" prefix. Byte/time budgets instead terminate with an error.
        pcm, _ = tools.ffmpeg_run(['-v', 'error', '-xerror', '-protocol_whitelist', 'file,pipe', '-format_whitelist', FORMAT_WHITELIST,
            '-i', str(path), '-map', f"0:{stream['index']}", '-vn', '-sn', '-dn', '-af', 'asetpts=PTS-STARTPTS',
            '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1'], timeout=timeout, stdout_limit=maximum+1024)
    except MediaError as exc:
        if exc.code=='output_budget':
            raise SimilarityError('audio_output_budget') from None
        raise
    if len(pcm) % 2 or not pcm:
        raise SimilarityError('audio_decode_failed')
    samples = np.frombuffer(pcm, dtype='<i2').astype(np.float64)/32768.0
    duration = len(samples)/16000.0
    if duration > max_seconds+.001:
        raise SimilarityError('audio_duration_budget')
    if sha256(path.read_bytes()).hexdigest() != source.content_sha256:
        raise SimilarityError('source_integrity')
    size, hop = 4096, 2048
    count = max(1, math.ceil(len(samples)/hop))
    padded = np.pad(samples, (0, max(0, (count-1)*hop+size-len(samples))))
    windows = np.stack([padded[i*hop:i*hop+size] for i in range(count)])
    rms = np.sqrt(np.mean(windows*windows, axis=1))
    active = rms >= .001  # Technical signal gate (-60 dBFS), not a duplicate threshold.
    power = np.abs(np.fft.rfft(windows*np.hanning(size), axis=1))**2
    frequencies = np.fft.rfftfreq(size, 1/16000.0)
    edges = np.geomspace(60.0, 7900.0, 25)
    bands = np.stack([power[:, (frequencies >= a) & (frequencies < b)].sum(axis=1) for a,b in zip(edges[:-1],edges[1:])], axis=1)
    sums = bands.sum(axis=1, keepdims=True)
    bands = np.sqrt(np.divide(bands, sums, out=np.zeros_like(bands), where=sums > 1e-16))
    bands[~active] = 0.0
    group = max(1, math.ceil(len(bands)/512))
    grouped = np.stack([bands[i:i+group].mean(axis=0) for i in range(0, len(bands), group)])
    norms = np.linalg.norm(grouped, axis=1, keepdims=True)
    grouped = np.divide(grouped, norms, out=np.zeros_like(grouped), where=norms > 1e-12)
    origin = stream.get('start_time')
    try:
        origin = float(origin) if origin is not None else None
    except (TypeError, ValueError):
        origin = None
    return AudioFingerprint(source=source, source_stream_index=int(stream['index']),
        source_origin_seconds=origin if origin is None or math.isfinite(origin) else None,
        decoded_duration_seconds=duration, decoded_sample_count=len(samples),feature_step_seconds=hop*group/16000.0,
        spectral_frames=tuple(tuple(float(min(1.0, max(0.0, v))) for v in row) for row in grouped),
        active_fraction=float(active.mean()), decoder_identity='FFmpeg-'+sha256(json.dumps(tools.identity,sort_keys=True).encode()).hexdigest()+':NumPy-'+np.__version__)


def _visual_score(left, right):
    hamming = (int(left.dhash, 16)^int(right.dhash, 16)).bit_count()/256.0
    alpha = (int(left.alpha_hash, 16)^int(right.alpha_hash, 16)).bit_count()/256.0
    colour = math.sqrt(sum((a-b)**2 for a,b in zip(left.mean_rgb,right.mean_rgb)))/(255*math.sqrt(3))
    grid = sum(abs(a-b) for a,b in zip(left.luma_grid,right.luma_grid))/(64*255)
    return max(0.0, min(1.0, 1-(.45*hamming+.2*alpha+.2*grid+.15*colour)))


def compare(left, right):
    if (type(left) is not type(right) or left.algorithm_version != right.algorithm_version
            or left.algorithm_version not in (VISUAL_VERSION,AUDIO_VERSION,CLIP_VERSION)):
        raise SimilarityError('incompatible_descriptor_profiles')
    common = dict(left=left.source, right=right.source, left_descriptor_sha256=left.digest,
        right_descriptor_sha256=right.digest, algorithm_version=left.algorithm_version)
    if left.source.content_sha256 == right.source.content_sha256:
        return Candidate(**common, comparison='exact_bytes', score=1.0)
    if isinstance(left, ClipFingerprint):
        if left.model_revision != right.model_revision or left.preprocess_version != right.preprocess_version:
            raise SimilarityError('incompatible_descriptor_profiles')
        def cosine(a,b):
            na,nb=math.hypot(*a),math.hypot(*b)
            return sum((x/na)*(y/nb) for x,y in zip(a,b))
        score=max(cosine(a,b) for a in left.vectors for b in right.vectors)
        return Candidate(**common,comparison='visual_overlap',score=max(0.0,min(1.0,(score+1.0)/2.0)))
    if isinstance(left, VisualFingerprint):
        # Actual visual overlap only. Sparse samples cannot prove whole-video identity.
        if len(left.frames) == 1 or len(right.frames) == 1:
            score = max(_visual_score(a,b) for a in left.frames for b in right.frames)
        else:
            count = max(len(left.frames),len(right.frames))
            score = sum(_visual_score(left.frames[round(i*(len(left.frames)-1)/(count-1))],
                right.frames[round(i*(len(right.frames)-1)/(count-1))]) for i in range(count))/count
        return Candidate(**common, comparison='visual_overlap', score=float(score))
    if min(left.active_fraction, right.active_fraction) < .05:
        raise SimilarityError('insufficient_audio_signal')
    try:
        import numpy as np
    except ImportError:
        raise SimilarityError('audio_numpy_extra_unavailable') from None
    step = max(left.feature_step_seconds,right.feature_step_seconds)
    def matrix(descriptor):
        values = np.array(descriptor.spectral_frames)
        times = np.arange(0, descriptor.decoded_duration_seconds, step)
        indices = np.minimum(len(values)-1, np.rint(times/descriptor.feature_step_seconds).astype(int))
        return values[indices]
    a,b = matrix(left),matrix(right)
    minimum = max(1, min(len(a),len(b), math.ceil(min(1.0, min(left.decoded_duration_seconds,right.decoded_duration_seconds))/step)))
    minimum = max(minimum, math.ceil(.8*min(len(a),len(b))))
    best = (-1.0, 0, 0)
    for lag in range(-len(b)+minimum, len(a)-minimum+1):
        ai,bi = max(0,lag),max(0,-lag)
        length = min(len(a)-ai,len(b)-bi)
        if length < minimum:
            continue
        aa,bb = a[ai:ai+length],b[bi:bi+length]
        information = (np.linalg.norm(aa,axis=1) > .1) & (np.linalg.norm(bb,axis=1) > .1)
        if int(information.sum()) < max(1, math.ceil(.5*length)):
            continue
        score = float(np.mean(np.sum(aa[information]*bb[information],axis=1)))
        if score > best[0]:
            best = (score,lag,length)
    if best[0] < 0:
        raise SimilarityError('insufficient_audio_overlap')
    overlap=min(best[2]*step,left.decoded_duration_seconds-max(0,best[1])*step,
        right.decoded_duration_seconds-max(0,-best[1])*step)
    if overlap <= 0:
        raise SimilarityError('insufficient_audio_overlap')
    return Candidate(**common, comparison='audio_overlap', score=float(min(1.0,max(0.0,best[0]))),
        estimated_right_offset_seconds=float(best[1]*step), overlap_seconds=float(overlap))


def ranking_report(query, pool, *, limit=20):
    if not 1 <= limit <= 100 or len(pool) > 1000:
        raise SimilarityError('candidate_pool_budget')
    output,skipped = [],[]
    for item in pool:
        if item.source == query.source:
            continue
        try:
            output.append(compare(query,item))
        except SimilarityError as exc:
            if exc.code not in ('insufficient_audio_signal','insufficient_audio_overlap','incompatible_descriptor_profiles'):
                raise
            skipped.append({'source':item.source.model_dump(mode='json'),'reason':exc.code})
    items=sorted(output,key=lambda value:(-value.score,value.right.asset_id,value.right_descriptor_sha256))[:limit]
    return {'query':query.source.model_dump(mode='json'),'state':'partial' if items and skipped else 'ranked' if items else 'insufficient_information' if skipped else 'no_candidates',
        'items':items,'skipped':skipped,'calibrated':False,'duplicate_confirmation':False}


def ranked(query, pool, *, limit=20):
    """Items-only convenience; orchestration should retain ranking_report diagnostics."""
    return ranking_report(query,pool,limit=limit)['items']
