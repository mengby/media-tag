"""Persist actual media descriptors through the existing Router and analysis intent."""
from hashlib import sha256
import json
import time
from importlib.metadata import PackageNotFoundError, version
import os
from typing import Literal

from media_tag.contracts.models import ArtifactRef, ContractModel, Sha256, TaskEnvelope, WorkerResult, validate_result_for_task
from media_tag.models.contracts import ModelFailure
from media_tag.precision.runtime import read_json
from media_tag.router.models import StageDefinition
from media_tag.workers.evidence import _directory

from .contracts import AUDIO_VERSION, VISUAL_VERSION, AudioFingerprint, SourceVersion, VisualFingerprint
from .engines import SimilarityError, audio_bytes, video_bytes, visual_bytes

VERSION = 'ct-similarity-queue-v1'


class Deployment(ContractModel):
    enabled: bool = False


class Options(ContractModel):
    content_kind: Literal['image', 'animation', 'video', 'audio']
    tool_digest: Sha256


class Report(ContractModel):
    schema_version: Literal['ct-similarity-queue-v1'] = VERSION
    descriptor: VisualFingerprint | AudioFingerprint
    source: ArtifactRef
    input_fingerprint: Sha256
    tool_digest: Sha256
    duplicate_confirmed: Literal[False] = False
    labels_copied: Literal[False] = False
    medical_confirmation: Literal[False] = False


def tool_digest(tools):
    try:
        numpy_version = version('numpy')
    except PackageNotFoundError:
        numpy_version = 'unavailable'
    return sha256(json.dumps({'tools': tools.identity, 'numpy': numpy_version}, sort_keys=True).encode()).hexdigest()


def stage(tools):
    return StageDefinition(Options, VERSION+':'+tool_digest(tools), metadata_sensitive=False,
        required_preprocess_version=VERSION, required_model_revision=None,
        requires_no_model=True, allowed_target_fields=(),
        reduce_config=lambda value: value, required_gpu_slots=0)


class Handlers:
    def __init__(self, router, principal, tools):
        self.router, self.principal, self.tools = router, principal, tools

    def __call__(self, lease, stopped):
        task = lease.task
        options = Options.model_validate(lease.config)
        deadline = time.monotonic()+task.limits.timeout_seconds-1

        def check():
            if stopped.is_set():
                raise ModelFailure('cancelled')
            if time.monotonic() >= deadline:
                raise ModelFailure('similarity_timeout')

        check()
        if options.tool_digest != tool_digest(self.tools) or task.versions.preprocess_version != VERSION:
            raise ModelFailure('similarity_configuration_changed')
        probes = [ref for ref in task.input_refs if ref.kind == 'technical_report']
        if len(probes) != 1:
            raise ModelFailure('similarity_verified_probe_required')
        kind = read_json(self.router, probes[0])['kind']
        if {'gif': 'animation'}.get(kind, kind) != options.content_kind:
            raise ModelFailure('similarity_modality_changed')
        chunks, size = [], 0
        with self.router.open_input(task.task_id, task.lease_epoch, principal=self.principal) as stream:
            while chunk := stream.read(1024**2):
                check()
                size += len(chunk)
                if size > 128*1024**2:
                    raise ModelFailure('similarity_input_budget')
                chunks.append(chunk)
        data = b''.join(chunks)
        if sha256(data).hexdigest() != task.input_refs[0].sha256:
            raise ModelFailure('similarity_source_integrity')
        folder = _directory(self.router.workspace, task.output_prefix.rstrip('/'))
        (folder/'README.md').write_text('Private similarity source and descriptor; uncalibrated candidates only.\n')
        path = folder/'source.bin'
        with path.open('xb') as stream:
            stream.write(data)
        source = SourceVersion(asset_id=task.asset_id, asset_revision=task.asset_revision,
            content_sha256=task.input_refs[0].sha256)
        # Every native operation is bounded by the remaining lease time. No quota
        # is released until the handler and its native commands have returned.
        parent_tools = self.tools

        class RemainingTools:
            identity = parent_tools.identity

            def probe_run(self, *args, timeout, **kwargs):
                check()
                return parent_tools.probe_run(*args, timeout=min(timeout, max(.01, deadline-time.monotonic())), **kwargs)

            def ffmpeg_run(self, *args, timeout, **kwargs):
                check()
                return parent_tools.ffmpeg_run(*args, timeout=min(timeout, max(.01, deadline-time.monotonic())), **kwargs)

        tools = RemainingTools()
        check()
        try:
            if options.content_kind in ('image', 'animation'):
                descriptor = visual_bytes(data, source)
            elif options.content_kind == 'audio':
                descriptor = audio_bytes(data, source, folder/'decoded', tools)
            else:
                descriptor = video_bytes(data, source, folder/'decoded', tools)
        except SimilarityError as exc:
            raise ModelFailure(exc.code) from None
        check()
        ref = ArtifactRef(kind='similarity_input', ref=path.relative_to(self.router.workspace).as_posix(), sha256=source.content_sha256)
        report = Report(descriptor=descriptor, source=ref, input_fingerprint=task.input_fingerprint, tool_digest=options.tool_digest)
        report_path = folder/'similarity.json'
        report_path.write_text(report.model_dump_json(), encoding='utf8')
        report_ref = ArtifactRef(kind='similarity_report', ref=report_path.relative_to(self.router.workspace).as_posix(),
            sha256=sha256(report_path.read_bytes()).hexdigest())
        return {'status': 'succeeded', 'artifacts': (ref, report_ref)}


def verified(router, row, asset):
    task = TaskEnvelope.model_validate_json(row['envelope_json'])
    result = WorkerResult.model_validate_json(row['result_json'])
    options = Options.model_validate_json(json.dumps(json.loads(row['spec_json'])['config']))
    current = router.stages.get('similarity')
    if (row['state'] != 'succeeded' or row['released_at'] is None or task.stage != 'similarity'
            or task.task_id != row['task_id'] or task.lease_epoch != row['lease_epoch']
            or task.job_id != row['job_id'] or task.asset_id != asset['asset_id']
            or task.input_refs[0].sha256 != asset['blob_sha256'] or result.status != 'succeeded'
            or current is None or current.implementation_version != VERSION+':'+options.tool_digest
            or task.versions.preprocess_version != VERSION or result.suggestions or result.evidence):
        raise ValueError('current similarity attempt required')
    validate_result_for_task(task, result)
    if len(result.artifacts) != 2 or {ref.kind for ref in result.artifacts} != {'similarity_report', 'similarity_input'}:
        raise ValueError('exactly one similarity source and report required')
    # Generic Router._artifacts hashes to EOF. Do not use it for this bounded
    # read API: a corrupt/growing private artifact must not evade source budgets.
    with router.database.transaction() as connection:
        size = connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?', (asset['blob_sha256'],)).fetchone()[0]
    if size > 128*1024**2:
        raise ValueError('similarity source byte budget')
    for artifact in result.artifacts:
        if artifact.kind not in ('similarity_report', 'similarity_input') or not artifact.ref.startswith(task.output_prefix):
            raise ValueError('similarity attempt output required')
        maximum = 4*1024**2 if artifact.kind == 'similarity_report' else size
        with router.files.open_read(artifact.ref) as stream:
            initial = os.fstat(stream.fileno())
            if initial.st_size > maximum or artifact.kind == 'similarity_input' and initial.st_size != size:
                raise ValueError('similarity artifact byte budget')
            digest, consumed = sha256(), 0
            while chunk := stream.read(min(1024**2, maximum-consumed+1)):
                consumed += len(chunk)
                if consumed > maximum:
                    raise ValueError('similarity artifact grew beyond byte budget')
                digest.update(chunk)
            if digest.hexdigest() != artifact.sha256 or os.fstat(stream.fileno()).st_size != initial.st_size:
                raise ValueError('similarity artifact changed')
    refs = [ref for ref in result.artifacts if ref.kind == 'similarity_report']
    if len(refs) != 1:
        raise ValueError('one similarity report required')
    report = Report.model_validate_json(json.dumps(read_json(router, refs[0])))
    probes = [ref for ref in task.input_refs if ref.kind == 'technical_report']
    if len(probes) != 1:
        raise ValueError('current verified similarity probe required')
    info = read_json(router, probes[0])
    kind = info['kind']
    if {'gif': 'animation'}.get(kind, kind) != options.content_kind:
        raise ValueError('similarity input modality changed')
    descriptor = report.descriptor
    expected_algorithm = AUDIO_VERSION if options.content_kind == 'audio' else VISUAL_VERSION
    if (len(result.artifacts) != 2 or report.source not in result.artifacts or report.source.kind != 'similarity_input'
            or report.source.sha256 != asset['blob_sha256'] or report.input_fingerprint != task.input_fingerprint
            or report.tool_digest != options.tool_digest or descriptor.source.asset_id != task.asset_id
            or descriptor.source.asset_revision != task.asset_revision
            or descriptor.source.content_sha256 != asset['blob_sha256'] or descriptor.algorithm_version != expected_algorithm
            or isinstance(descriptor, AudioFingerprint) != (options.content_kind == 'audio')):
        raise ValueError('similarity source or algorithm binding changed')
    if isinstance(descriptor, VisualFingerprint) and descriptor.media_kind != options.content_kind:
        single_gif = (kind == 'gif' and info.get('raw_metadata', {}).get('image_metadata', {}).get('frame_count') == 1 and options.content_kind == 'animation'
            and descriptor.media_kind == 'image' and descriptor.coverage == 'complete_image' and len(descriptor.frames) == 1)
        if not single_gif:
            raise ValueError('similarity visual modality changed')
    return descriptor
