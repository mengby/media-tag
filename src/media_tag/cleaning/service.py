"""Read-only source quality checks, stable copies and physical clip outputs."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from PIL import Image, ImageFilter, ImageStat
from pydantic import TypeAdapter

from media_tag.contracts.models import Identifier
from media_tag.storage import StorageError
from media_tag.storage.filesystem import FilesystemBackend, require_local_path
from media_tag.workers.evidence import _directory
from media_tag.workers.probe import ProbeOptions, file_sha256, probe_media

from .models import CleaningLimits
from .errors import CleaningError, CleaningPending
from .video import plan_video, render_video

VERSION = 'ct-cleaning-core-v2'


def _fixed_metadata():
    return {'original_preserved': True, 'quality_confirmation': False, 'privacy_confirmation': False,
        'medical_confirmation': False, 'license_status': 'unverified', 'semantic_labels_generated': False,
        'face_redaction_applied': False, 'watermark_removal_applied': False}


def _validate_fixed_metadata(report):
    if any(type(report.get(key)) is not type(expected) or report[key] != expected
            for key, expected in _fixed_metadata().items()):
        raise CleaningError('cached reports cannot supply approvals or unsupported capabilities', code='cache_approval_or_capability_invalid')
    quality = report.get('source_quality')
    if not isinstance(quality, dict) or any(quality.get(key, False) is not False
            for key in ('quality_approval', 'semantic_labels_generated', 'audio_semantics_generated')):
        raise CleaningError('cached quality cannot claim approval or semantics', code='cache_approval_or_capability_invalid')


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _write_json(path, value):
    temporary = path.with_name(path.name+'.tmp-'+uuid4().hex)
    with temporary.open('x', encoding='utf8') as stream:
        stream.write(_json(value))
    os.replace(temporary, path)


def _copy_bounded(source, target, maximum):
    digest, count = sha256(), 0
    while chunk := source.read(1024**2):
        count += len(chunk)
        if count > maximum:
            raise CleaningError('copy byte budget exceeded')
        digest.update(chunk)
        target.write(chunk)
    return digest.hexdigest(), count


def _archive(path):
    if path.exists() or path.is_symlink():
        path.rename(path.with_name(path.stem+'.history-'+uuid4().hex+path.suffix))


def _suffix(info):
    image = info['raw_metadata'].get('image_metadata')
    if image:
        return {'PNG': '.png', 'JPEG': '.jpg', 'GIF': '.gif', 'WEBP': '.webp', 'BMP': '.bmp', 'TIFF': '.tif'}[image['format']]
    names = set(info['raw_metadata']['format']['format_name'].split(','))
    for name, suffix in [('wav', '.wav'), ('mp3', '.mp3'), ('flac', '.flac'), ('ogg', '.ogg'),
                         ('aiff', '.aiff'), ('aac', '.aac'), ('mp4', '.m4a'), ('matroska', '.mka')]:
        if name in names:
            return suffix
    raise CleaningError('supported copy container extension unavailable')


def image_quality(path, limits):
    frames = []
    pixels = 0
    with Image.open(path) as image:
        loop = image.info.get('loop')
        for index in range(limits.max_image_frames+1):
            try:
                image.seek(index)
            except EOFError:
                break
            if index >= limits.max_image_frames:
                raise CleaningError('image/animation frame budget exceeded')
            area = image.width*image.height
            pixels += area
            if area > limits.max_pixels or pixels > limits.max_image_pixels_total:
                raise CleaningError('image/animation decoded pixel budget exceeded')
            image.load()
            rgba = image.convert('RGBA')
            alpha = rgba.getchannel('A')
            edges = rgba.convert('L').filter(ImageFilter.FIND_EDGES)
            frames.append({'index': index, 'duration_ms': image.info.get('duration'),
                'width': image.width, 'height': image.height, 'alpha_sha256': sha256(alpha.tobytes()).hexdigest(),
                'has_transparent_pixels': alpha.getextrema()[0] < 255,
                'edge_variance': ImageStat.Stat(edges).var[0]})
    return {'full_decode': True, 'frame_count': len(frames), 'loop': loop, 'frames': frames,
        'decoded_pixels': pixels, 'sharpness_metric_calibrated': False,
        'quality_approval': False, 'semantic_labels_generated': False}


class CleaningService:
    def __init__(self, workspace, tools, *, limits=None):
        workspace = Path(workspace)
        if not workspace.is_absolute():
            raise CleaningError('absolute local workspace required')
        require_local_path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        self.workspace = FilesystemBackend(workspace, writable=True).root
        self.tools, self.limits = tools, limits or CleaningLimits()
        if not (self.workspace/'README.md').exists():
            (self.workspace/'README.md').write_text('Private cleaning jobs and cache, not a published media directory.\n')
        self.cache = _directory(self.workspace, 'cleaning-cache')

    def _source(self, source, expected):
        source = Path(source)
        if not source.is_absolute():
            raise CleaningError('absolute local source required')
        require_local_path(source)
        if any(source.resolve().is_relative_to(root) for root in (self.workspace/'jobs', self.cache)):
            raise CleaningError('generated cleaning outputs cannot silently become a new source time base')
        backend = FilesystemBackend(source.parent)
        with backend.open_read(source.name) as stream:
            if os.fstat(stream.fileno()).st_size > self.limits.max_input_bytes:
                raise CleaningError('source byte budget exceeded')
            digest = sha256()
            count = 0
            while chunk := stream.read(1024**2):
                count += len(chunk)
                if count > self.limits.max_input_bytes:
                    raise CleaningError('source byte budget exceeded while hashing')
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise CleaningError('source hash differs from explicit recipe')
        return source.resolve()

    def _cached(self, bundle, key, recipe):
        backend = FilesystemBackend(bundle)
        with backend.open_read('report.json') as stream:
            raw = stream.read(16*1024**2+1)
        with backend.open_read('report.sha256') as stream:
            expected = stream.read(129).decode('ascii').strip()
        if len(raw) > 16*1024**2 or sha256(raw).hexdigest() != expected:
            raise CleaningError('cached report hash mismatch')
        report = json.loads(raw)
        if not isinstance(report, dict):
            raise CleaningError('cached report must be an object')
        if report.get('cache_key') != key or report.get('schema_version') != VERSION:
            raise RuntimeError('cache identity mismatch; do not replace a conflicting bundle')
        if (report.get('source_sha256') != recipe.source_sha256 or report.get('recipe') != recipe.model_dump(mode='json')
                or report.get('limits') != self.limits.model_dump(mode='json') or report.get('tools') != self.tools.identity
                or report.get('content_kind') != recipe.content_kind or type(report.get('output_bytes')) is not int
                or not 1 <= report['output_bytes'] <= self.limits.max_output_bytes):
            raise CleaningError('cached report source/rules binding mismatch')
        _validate_fixed_metadata(report)
        with backend.open_read(report['artifact_name']) as stream:
            if os.fstat(stream.fileno()).st_size != report['output_bytes']:
                raise CleaningError('cached artifact size mismatch')
        with backend.open_verified(report['artifact_name'], report['output_sha256'], report['output_bytes']):
            pass
        return report

    def _discard_staging(self, staging):
        staging = Path(staging)
        cache = self.cache.resolve()
        actual = staging.resolve()
        if (cache.parent != self.workspace or cache.name != 'cleaning-cache' or actual == cache
                or actual.parent != cache or staging.parent.resolve() != cache or actual.name != staging.name
                or re.fullmatch(r'staging-[0-9a-f]{32}', staging.name) is None):
            raise CleaningError('staging cleanup target is not a generated direct child', code='staging_cleanup_target_invalid')
        shutil.rmtree(staging)

    def run(self, source, recipe, *, job_id):
        """Trusted local caller supplies an authorized original; no catalogue rights are granted."""
        started = time.monotonic()
        TypeAdapter(Identifier).validate_python(job_id)
        source = self._source(source, recipe.source_sha256)
        key = sha256(_json({'version': VERSION, 'recipe': recipe.model_dump(mode='json'),
            'limits': self.limits.model_dump(mode='json'), 'tools': self.tools.identity}).encode()).hexdigest()
        job = _directory(self.workspace, f'jobs/{job_id}/cleaning')
        request = {'cache_key': key, 'original_name': source.name, 'recipe': recipe.model_dump(mode='json')}
        request_path = job/'request.json'
        if request_path.exists():
            with FilesystemBackend(job).open_read('request.json') as stream:
                old = json.load(stream)
            if old != request:
                raise CleaningError('job ID reused with a different source name or recipe')
        try:
            if not request_path.exists():
                _write_json(request_path, request)
            _write_json(job/'status.json', {'state': 'checking', 'cache_key': key, 'source_sha256': recipe.source_sha256})
            result = self._adopt(source, recipe, key=key, job=job, job_id=job_id, started=started)
            _write_json(job/'result.json', result)
            _write_json(job/'status.json', {'state': 'succeeded', 'cache_key': key, 'source_sha256': recipe.source_sha256,
                'output_sha256': result['output_sha256'], 'job_elapsed_seconds': result['job_elapsed_seconds']})
            _archive(job/'failure.json')
            return result
        except Exception as exc:
            # A previous successful receipt is history, not a current success after
            # this adoption/copy/publication attempt failed or is waiting for a choice.
            _archive(job/'result.json')
            state = getattr(exc, 'state', 'failed')
            if state not in ('waiting_policy', 'waiting_evidence'):
                state = 'failed'
            outcome = {'state': state, 'error_type': type(exc).__name__, 'code': getattr(exc, 'code', 'cleaning_failed'),
                'source_sha256': recipe.source_sha256, 'cache_key': key, 'production_operation': False,
                'job_elapsed_seconds': time.monotonic()-started}
            _write_json(job/'status.json', outcome)
            if state == 'failed':
                _write_json(job/'failure.json', outcome)
            else:
                _archive(job/'failure.json')
            raise

    def _adopt(self, source, recipe, *, key, job, job_id, started):
        """All cache selection, source rechecks and copying are inside run's outcome boundary."""
        bundle = self.cache/key[:24]
        reused, repaired = False, False
        if bundle.exists():
            try:
                report = self._cached(bundle, key, recipe)
                reused = True
            except (StorageError, OSError, ValueError, KeyError):
                # Keep invalid generated evidence; never overwrite or move an original.
                if bundle.parent.resolve() != self.cache.resolve() or not bundle.resolve().is_relative_to(self.cache):
                    raise CleaningError('cache quarantine target escaped workspace')
                bundle.rename(self.cache/f'invalid-{key[:12]}-{uuid4().hex[:8]}')
                repaired = True
        if not reused:
            staging = _directory(self.cache, 'staging-'+uuid4().hex)
            report = self._produce(source, recipe, staging, key)
            self._source(source, recipe.source_sha256)
            _write_json(staging/'report.json', report)
            (staging/'report.sha256').write_text(file_sha256(staging/'report.json')+'\n', encoding='ascii')
            try:
                staging.rename(bundle)
            except FileExistsError:
                report = self._cached(bundle, key, recipe)
                self._discard_staging(staging)
                reused = True
        self._source(source, recipe.source_sha256)
        name = f"asset-{recipe.source_sha256[:20]}-{key[:16]}{Path(report['artifact_name']).suffix}"
        target = job/name
        if target.exists():
            try:
                with FilesystemBackend(job).open_verified(name, report['output_sha256'], report['output_bytes']):
                    pass
            except (StorageError, OSError):
                target.rename(job/(name+'.invalid-'+uuid4().hex[:8]))
        if not target.exists():
            with FilesystemBackend(bundle).open_verified(report['artifact_name'], report['output_sha256'], report['output_bytes']) as stream, target.open('xb') as out:
                _copy_bounded(stream, out, self.limits.max_output_bytes)
        if file_sha256(target) != report['output_sha256']:
            raise CleaningError('job output hash mismatch')
        self._source(source, recipe.source_sha256)
        result = {**report, 'job_id': job_id, 'cache_reused': reused, 'invalid_cache_rebuilt': repaired,
            'job_elapsed_seconds': time.monotonic()-started,
            'original_name': source.name, 'standardized_name': name,
            'output_ref': target.relative_to(self.workspace).as_posix(),
            'name_mapping': {'original_name': source.name, 'standardized_name': name,
                'source_sha256': recipe.source_sha256, 'output_sha256': report['output_sha256']}}
        result.update(_fixed_metadata())
        return result

    def _produce(self, source, recipe, staging, key):
        started = time.monotonic()
        timings = {}
        # All decoders read a private, hash-verified snapshot, never a pathname that
        # an outside uploader might replace between the probe and actual encode.
        snapshot = staging/'source-input.bin'
        with FilesystemBackend(source.parent).open_read(source.name) as original, snapshot.open('xb') as copied:
            snapshot_sha, _ = _copy_bounded(original, copied, self.limits.max_input_bytes)
        if snapshot_sha != recipe.source_sha256:
            raise CleaningError('source changed while creating the private decode snapshot')
        self._source(source, recipe.source_sha256)
        source = snapshot
        timings['snapshot_seconds'] = time.monotonic()-started
        stage_started = time.monotonic()
        options = ProbeOptions(max_input_bytes=self.limits.max_input_bytes, max_pixels=self.limits.max_pixels,
            max_alpha_frames=self.limits.max_image_frames, max_alpha_pixels=self.limits.max_image_pixels_total,
            timeout_seconds=min(120.0, self.limits.timeout_seconds))
        info = probe_media(source, self.tools, options).as_dict()
        timings['probe_seconds'] = time.monotonic()-stage_started
        stage_started = time.monotonic()
        from media_tag.analysis.probe import semantic_content_kind
        actual = semantic_content_kind(info)
        if actual != recipe.content_kind:
            raise CleaningError('actual source kind differs from explicit recipe')
        quality = {'width': info['width'], 'height': info['height'], 'has_audio': info['has_audio'],
            'audio_tracks': list(info['audio_tracks']), 'probe_warnings': list(info['warnings'])}
        time_map = None
        if actual == 'video':
            plan = plan_video(source, self.tools, info, recipe, self.limits)
            target = staging/'media.mp4'
            checks = render_video(source, target, self.tools, recipe, plan, self.limits)
            quality.update(checks)
            time_map = plan
            mode = 'physical_clip_and_explicit_retime' if recipe.segment is not None else 'video_standardization'
            duration_state = 'in_3_to_5' if 3.0-.0001 <= plan['output_duration_seconds'] <= 5.0+.0001 else 'requires_duration_decision'
        else:
            if actual in ('image', 'animation'):
                quality.update(image_quality(source, self.limits))
            else:
                durations = [track['duration_seconds'] for track in info['audio_tracks']]
                if not durations or any(d is None or not 0 < d <= self.limits.max_seconds for d in durations):
                    raise CleaningPending('known bounded audio track durations required', code='audio_clock_unavailable', state='waiting_evidence')
                self.tools.ffmpeg_run(['-v', 'error', '-xerror', '-i', str(source), '-map', '0:a', '-f', 'null', '-'], timeout=self.limits.timeout_seconds)
                quality.update(full_decode=True, track_count=len(durations), audio_semantics_generated=False)
            target = staging/('media'+_suffix(info))
            with FilesystemBackend(source.parent).open_read(source.name) as stream, target.open('xb') as out:
                _copy_bounded(stream, out, self.limits.max_output_bytes)
            mode, duration_state = 'encoded_bytes_preserved', 'not_a_video_duration_rule'
        if target.stat().st_size > self.limits.max_output_bytes:
            raise CleaningError('output byte budget exceeded')
        timings['quality_and_output_seconds'] = time.monotonic()-stage_started
        result = {'schema_version': VERSION, 'cache_key': key, 'source_sha256': recipe.source_sha256,
            'output_sha256': file_sha256(target), 'output_bytes': target.stat().st_size,
            'artifact_name': target.name, 'recipe': recipe.model_dump(mode='json'), 'tools': self.tools.identity,
            'limits': self.limits.model_dump(mode='json'),
            'content_kind': actual, 'transformation': mode, 'time_map': time_map,
            'duration_target_state': duration_state, 'source_quality': quality,
            'stage_timings': timings,
            'segments_are_annotations_only': False if actual == 'video' else None,
            **_fixed_metadata()}
        if file_sha256(snapshot) != recipe.source_sha256:
            raise CleaningError('private source snapshot changed during processing')
        snapshot.unlink()
        return result
