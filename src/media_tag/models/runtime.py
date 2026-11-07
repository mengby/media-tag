"""Lease-bound, subprocess-isolated OCR/face/CLIP stages. No direct catalogue writes by models."""

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from media_tag.contracts.models import ArtifactRef, ErrorDetail, Evidence
from media_tag.router.models import StageDefinition
from media_tag.workers.evidence import _directory
from .contracts import CLIPOptions, FaceOptions, ModelFailure, OCROptions
from .registry import ModelRegistry

PREPROCESS_VERSION = 'd04-frames-v1'


def model_stages(registry, *, clip_device='cuda'):
    if clip_device not in ('cuda', 'cpu'):
        raise ValueError('explicit clip device required')
    return {kind: StageDefinition(model, f'd04-{kind}-v1:{registry.revision}:{clip_device if kind == "clip" else "cpu"}',
             gpu_allowed=kind == 'clip' and clip_device == 'cuda', metadata_sensitive=False,
             required_model_revision=registry.revision, required_preprocess_version=PREPROCESS_VERSION,
             required_gpu_slots=int(kind == 'clip' and clip_device == 'cuda'), allowed_target_fields=())
            for kind, model in [('ocr', OCROptions), ('face', FaceOptions), ('clip', CLIPOptions)]}


def read_json(backend, artifact, *, limit=4 * 1024**2):
    with backend.open_read(artifact.ref) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or sha256(raw).hexdigest() != artifact.sha256:
        raise ModelFailure('input_manifest_integrity')
    return json.loads(raw)


class ModelHandlers:
    def __init__(self, router, principal, registry, *, clip_device='cuda'):
        self.router, self.principal, self.registry, self.clip_device = router, principal, registry, clip_device

    def __call__(self, lease, stopped):
        task = lease.task
        if stopped.is_set():
            raise ModelFailure('cancelled')
        self.registry.verify(task.stage)
        if task.versions.model_revision != self.registry.revision or task.versions.preprocess_version != PREPROCESS_VERSION:
            raise ModelFailure('model_revision_mismatch')
        manifests = [ref for ref in task.input_refs if ref.kind == 'evidence_manifest']
        if len(manifests) != 1:
            raise ModelFailure('missing_evidence_manifest')
        data = read_json(self.router.files, manifests[0])
        if data['status'] == 'not_applicable':
            frames = []
        elif data['status'] == 'succeeded':
            frames = []
            for frame in data['frames']:
                ref = ArtifactRef(**{key: frame['artifact'][key] for key in ('kind', 'ref', 'sha256')})
                if ref not in task.input_refs or frame['source_sha256'] != task.input_refs[0].sha256:
                    raise ModelFailure('frame_binding_mismatch')
                frames.append({'artifact': ref.model_dump(), 'source_sha256': frame['source_sha256'], 'normalized_seconds': frame['normalized_seconds']})
        else:
            raise ModelFailure('evidence_unavailable')
        if len(frames) > lease.config.get('max_frames', 12):
            raise ModelFailure('frame_budget')
        parent = _directory(self.router.workspace, task.output_prefix.rstrip('/').rsplit('/', 1)[0])
        output = parent / f'attempt-{task.attempt}'; output.mkdir()
        (output / 'README.md').write_text('Private D04 model evidence. No published index, privacy or medical approval.\n', encoding='utf-8')
        (output / 'task.json').write_text(task.model_dump_json(indent=2), encoding='utf-8')
        target = output / 'model-report.json'
        if not frames:
            target.write_text(json.dumps({'state': 'not_applicable', 'reason': 'no_visual_evidence'}), encoding='utf-8')
            return {'status': 'not_applicable', 'reason': 'no_visual_evidence', 'artifacts': (self._artifact(target),)}
        request = {'kind': task.stage, 'model_root': str(self.registry.root.resolve()), 'model_revision': self.registry.revision,
                   'executor_pid': os.getpid(),
                   'workspace': str(self.router.workspace), 'source_sha256': task.input_refs[0].sha256, 'frames': frames,
                   'device': self.clip_device if task.stage == 'clip' else 'cpu', 'options': lease.config}
        request_path, result_path = output / 'request.json', output / 'response.json'
        request_path.write_text(json.dumps(request, sort_keys=True), encoding='utf-8')
        env = os.environ.copy()
        env.update({'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1',
                    'TOKENIZERS_PARALLELISM': 'false', 'OMP_NUM_THREADS': '1', 'PYTHONUTF8': '1'})
        started = time.monotonic()
        # No shell and no caller-provided command. Redirect raw native logs to private bounded-by-time files.
        with (output / 'native.stdout.log').open('wb') as stdout, (output / 'native.stderr.log').open('wb') as stderr:
            process = subprocess.Popen([sys.executable, '-m', 'media_tag.models.runner', str(request_path), str(result_path)],
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                while process.poll() is None:
                    if stopped.wait(0.1):
                        raise ModelFailure('cancelled')
                    if time.monotonic() - started >= task.limits.timeout_seconds:
                        raise ModelFailure('model_timeout')
                    if os.fstat(stdout.fileno()).st_size + os.fstat(stderr.fileno()).st_size > 2 * 1024**2:
                        raise ModelFailure('model_log_budget')
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait()  # Return only after the actual process/GPU context has stopped.
        if not result_path.is_file() or result_path.stat().st_size > 8 * 1024**2:
            raise ModelFailure('model_process_failed')
        response = json.loads(result_path.read_text(encoding='utf-8'))
        if not response.get('ok'):
            raise ModelFailure(response['error_code'])
        report = response['data']
        if report['model_revision'] != self.registry.revision or report['source_sha256'] != task.input_refs[0].sha256:
            raise ModelFailure('model_result_binding')
        target.write_text(json.dumps(report, allow_nan=False, sort_keys=True), encoding='utf-8')
        evidence = tuple(Evidence(evidence_id=f'model-frame-{index}', kind='frame' if frame['normalized_seconds'] is not None else 'image',
            source_sha256=task.input_refs[0].sha256, artifact=ArtifactRef(**frame['artifact']), start_seconds=frame['normalized_seconds'])
            for index, frame in enumerate(frames))
        payload = {'status': 'succeeded', 'artifacts': (self._artifact(target),), 'evidence': evidence,
                   'peak_memory_bytes': report['peak_process_bytes']}
        if task.stage == 'clip':
            payload.update(status='partial', errors=(ErrorDetail(code='unpublished_vocabulary', message='No published medical candidate vocabulary is configured'),))
        return payload

    def _artifact(self, path):
        return ArtifactRef(kind='model_report', ref=path.relative_to(self.router.workspace).as_posix(), sha256=sha256(path.read_bytes()).hexdigest())
