"""Real contained CPU execution and source-bound, unregistered output artifacts."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from media_tag.catalog import Principal
from media_tag.catalog.service import encode
from media_tag.cleaning.service import VERSION as CLEANING_VERSION, _validate_fixed_metadata
from media_tag.contracts.models import ArtifactRef, ErrorDetail, TaskEnvelope
from media_tag.image_processing.models import ProcessingResult as ImageResult
from media_tag.image_processing.service import VERSION as IMAGE_VERSION
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import StageDefinition
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.transforms.processes import ProcessTree
from media_tag.workers.evidence import _directory
from media_tag.workers.probe import file_sha256

from .models import CleaningTask, ImageTask, ProcessingDeployment, ProcessingReport, VERSION
from .service import source_gate


def implementation(tools):
    return VERSION+':'+sha256(encode({'cleaning': CLEANING_VERSION, 'image': IMAGE_VERSION, 'tools': tools.identity}).encode()).hexdigest()


def stages(tools):
    revision = implementation(tools)
    return {name: StageDefinition(model, revision, required_preprocess_version=revision,
        required_gpu_slots=0, allowed_target_fields=(), requires_no_model=True)
        for name, model in [('cleaning_output', CleaningTask), ('image_mask_output', ImageTask)]}


class ProcessingHandlers:
    def __init__(self, router, worker, tools, options=None):
        self.router, self.worker, self.tools = router, worker, tools
        self.options = options or ProcessingDeployment()

    def _source(self, task, config):
        with self.router.database.transaction() as connection:
            self.router._worker(connection, self.worker)
            if self.router._lease(connection, task.task_id, task.lease_epoch, self.worker) is None:
                raise ModelFailure('processing_lease_unavailable')
            attempt = connection.execute('''SELECT envelope_json FROM router_attempts
                WHERE task_id=? AND lease_epoch=? AND worker_id=?''',
                (task.task_id, task.lease_epoch, self.worker.actor_id)).fetchone()
            if attempt is None or TaskEnvelope.model_validate_json(attempt[0]) != task:
                raise ModelFailure('processing_lease_binding_changed')
            job = connection.execute('SELECT * FROM router_jobs WHERE job_id=?', (task.job_id,)).fetchone()
            if job['owner_id'] != config.requester_id or job['asset_id'] != task.asset_id:
                raise ModelFailure('processing_actor_binding_changed')
            asset = source_gate(self.router, connection, task.asset_id, Principal(actor_id=config.requester_id),
                config.source_sha256, config.expected_revision)
            if asset['owner_id'] != config.source_owner_id:
                raise ModelFailure('processing_source_owner_changed')
            return asset

    def __call__(self, lease, stopped):
        task = lease.task
        config_type = CleaningTask if task.stage == 'cleaning_output' else ImageTask
        config = config_type.model_validate_json(encode(lease.config))
        renderer = CLEANING_VERSION if config.operation == 'cleaning' else IMAGE_VERSION
        expected_limits = self.options.cleaning if config.operation == 'cleaning' else self.options.image
        if (not self.options.enabled or task.stage not in stages(self.tools)
                or config.limits != expected_limits or task.versions.preprocess_version != implementation(self.tools)
                or task.versions.rules_version != 'sha256:'+self.router.catalog.contract.fingerprint
                or task.versions.model_revision is not None or task.target_fields or task.limits.gpu_slots != 0
                or task.limits.timeout_seconds > self.options.timeout_seconds):
            raise ModelFailure('processing_configuration_changed')
        output = _directory(self.router.workspace, task.output_prefix.rstrip('/'))
        (output/'README.md').write_text('Private unregistered processing artifacts. No approved preview/download or original registration.\n')
        recipe_sha = sha256(encode(config.recipe.model_dump(mode='json')).encode()).hexdigest()
        deadline = time.monotonic()+task.limits.timeout_seconds-min(2.0, task.limits.timeout_seconds*.25)
        verified = False
        def check():
            if stopped.is_set():
                raise ModelFailure('cancelled')
            if time.monotonic() >= deadline:
                raise ModelFailure('processing_timeout')
        try:
            check()
            asset = self._source(task, config)
            token = sha256(f'{task.task_id}:{task.lease_epoch}'.encode()).hexdigest()[:24]
            work = _directory(self.router.workspace, 'processing-work/'+token)
            binding = {'task_id': task.task_id, 'lease_epoch': task.lease_epoch,
                'source_sha256': config.source_sha256, 'input_fingerprint': task.input_fingerprint}
            marker = work/'binding.json'
            if marker.exists():
                with FilesystemBackend(work).open_read('binding.json') as stream:
                    if json.load(stream) != binding:
                        raise ModelFailure('processing_workspace_binding_changed')
            else:
                with marker.open('x', encoding='utf8') as stream:
                    stream.write(encode(binding))
            inputs = _directory(work, 'input')
            source = inputs/'source.bin'
            temporary = inputs/('source-'+os.urandom(8).hex()+'.bin')
            digest, count = sha256(), 0
            with self.router.open_input(task.task_id, task.lease_epoch, principal=self.worker) as original, temporary.open('xb') as copied:
                while chunk := original.read(1024**2):
                    check()
                    count += len(chunk)
                    if count > config.limits.max_input_bytes:
                        raise ModelFailure('processing_input_budget')
                    digest.update(chunk); copied.write(chunk)
            if digest.hexdigest() != config.source_sha256 or config.source_sha256 != task.input_refs[0].sha256:
                raise ModelFailure('processing_source_integrity')
            os.replace(temporary, source)
            verified = True
            request = {'source': str(source), 'work': str(work), 'tool_dir': str(self.tools.ffmpeg.parent),
                'config': config.model_dump(mode='json'), 'start_gate': str(output/'start.ready')}
            response = self.invoke(request, output, deadline, stopped)
            check()
            self._source(task, config)
            if response.get('state') != 'succeeded':
                state = response.get('state')
                if state not in ('waiting_policy', 'waiting_evidence'):
                    state = 'failed'
                code = response.get('issue_code', 'processing_failed')
                if not isinstance(code, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,127}', code) is None:
                    code = 'processing_failed'
                return self._unresolved(task, config, recipe_sha, renderer, output, state, code, verified)
            raw = response['result']
            core = FilesystemBackend(work/'core')
            if config.operation == 'cleaning':
                _validate_fixed_metadata(raw)
                if (raw.get('schema_version') != CLEANING_VERSION or raw.get('source_sha256') != config.source_sha256
                        or raw.get('recipe') != config.recipe.model_dump(mode='json')
                        or raw.get('limits') != config.limits.model_dump(mode='json')
                        or raw.get('tools') != self.tools.identity or raw.get('content_kind') != config.recipe.content_kind
                        or raw.get('job_id') != 'processing' or type(raw.get('output_bytes')) is not int):
                    raise ModelFailure('processing_core_binding_changed')
                ref, expected_sha, size = raw['output_ref'], raw['output_sha256'], raw['output_bytes']
                if ref != 'jobs/processing/cleaning/'+raw['standardized_name']:
                    raise ModelFailure('processing_core_path_changed')
                summary = {'content_kind': raw['content_kind'], 'duration_target_state': raw['duration_target_state'],
                    'cache_reused': raw['cache_reused']}
                if raw['time_map']:
                    summary.update(output_duration_seconds=raw['time_map']['output_duration_seconds'],
                        speed=raw['time_map']['speed'], full_output_decode=raw['source_quality']['full_output_decode'])
            else:
                typed = ImageResult.model_validate_json(encode(raw))
                if (typed.source_sha256 != config.source_sha256 or typed.renderer_version != IMAGE_VERSION
                        or typed.recipe != config.recipe or typed.job_id != 'processing'):
                    raise ModelFailure('processing_core_binding_changed')
                ref, expected_sha = typed.output_ref, typed.output_sha256
                with core.open_read(ref) as stream:
                    size = os.fstat(stream.fileno()).st_size
                summary = {'frame_count': typed.frame_count, 'animation_frame_count': typed.animation_frame_count,
                    'display_width': typed.display_width, 'display_height': typed.display_height,
                    'mask_region_count': sum(len(frame.regions) for frame in typed.frames),
                    'watermark_quality_status': typed.watermark_quality_status}
            if not 0 < size <= config.limits.max_output_bytes:
                raise ModelFailure('processing_output_budget')
            target = output/('output'+Path(ref).suffix)
            with core.open_verified(ref, expected_sha, size) as original, target.open('xb') as copied:
                copied_size, copied_hash = 0, sha256()
                while chunk := original.read(1024**2):
                    check()
                    copied_size += len(chunk)
                    if copied_size > config.limits.max_output_bytes:
                        raise ModelFailure('processing_output_budget')
                    copied_hash.update(chunk); copied.write(chunk)
            if copied_size != size or copied_hash.hexdigest() != expected_sha or file_sha256(target) != expected_sha:
                raise ModelFailure('processing_output_integrity')
            self._source(task, config)
            core_path = output/'core-report.json'
            core_path.write_text(encode(raw), encoding='utf8')
            media_ref = self.artifact(target, 'processing_output')
            core_ref = self.artifact(core_path, 'processing_core_report')
            report = self._report(task, config, recipe_sha, renderer, 'pending_registration_review', verified,
                output=media_ref, output_bytes=size, core_report=core_ref, summary=summary)
            return self._payload(report, output, 'succeeded', extra=(media_ref, core_ref))
        except Exception as exc:
            code = exc.code if isinstance(exc, ModelFailure) else 'processing_execution_failed'
            return self._unresolved(task, config, recipe_sha, renderer, output, 'failed', code, verified)

    def _report(self, task, config, recipe_sha, renderer, state, verified, **values):
        ready = state == 'pending_registration_review'
        return ProcessingReport(task_id=task.task_id, job_id=task.job_id, lease_epoch=task.lease_epoch,
            asset_id=task.asset_id, asset_revision=config.expected_revision, source_sha256=config.source_sha256,
            input_fingerprint=task.input_fingerprint, recipe_sha256=recipe_sha, renderer_version=renderer,
            operation=config.operation, state=state, source_bytes_verified=verified,
            registration_status='pending_registration' if ready else 'not_produced',
            review_status='pending' if ready else 'not_produced', **values)

    def _unresolved(self, task, config, recipe_sha, renderer, output, state, code, verified):
        report = self._report(task, config, recipe_sha, renderer, state, verified, issue_code=code)
        return self._payload(report, output, 'partial' if state.startswith('waiting_') else 'failed')

    def _payload(self, report, output, status, extra=()):
        path = output/'processing.json'
        path.write_text(report.model_dump_json(), encoding='utf8')
        payload = {'status': status, 'artifacts': (self.artifact(path, 'processing_report'), *extra)}
        if status != 'succeeded':
            payload.update(reason=report.state, errors=(ErrorDetail(code=report.issue_code, message='Processing did not produce an adopted artifact', retryable=False),))
        return payload

    def artifact(self, path, kind):
        return ArtifactRef(kind=kind, ref=path.relative_to(self.router.workspace).as_posix(), sha256=file_sha256(path))

    def invoke(self, request, output, deadline, stopped):
        request_path, response_path = output/'request.json', output/'response.json'
        request_path.write_text(encode(request), encoding='utf8')
        names = ('SYSTEMROOT','WINDIR','PATH','TEMP','TMP','VIRTUAL_ENV','PYTHONPATH')
        environment = {key:value for key,value in os.environ.items() if key.upper() in names}
        environment.update(PYTHONUTF8='1', OMP_NUM_THREADS='1', HF_HUB_OFFLINE='1',
            TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1', TOKENIZERS_PARALLELISM='false')
        tree, process, assigned = ProcessTree(), None, False
        try:
            with (output/'native.stdout.log').open('wb') as stdout, (output/'native.stderr.log').open('wb') as stderr:
                process = subprocess.Popen([sys.executable, '-m', 'media_tag.processing_queue.runner', str(request_path), str(response_path)],
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, env=environment,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                tree.assign(process)
                assigned = True
                (output/'start.ready').write_text('assigned\n')
                while process.poll() is None:
                    if stopped.wait(.05):
                        raise ModelFailure('cancelled')
                    if time.monotonic() >= deadline:
                        raise ModelFailure('processing_timeout')
                    if os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size > 2*1024**2:
                        raise ModelFailure('processing_log_budget')
                if os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size > 2*1024**2:
                    raise ModelFailure('processing_log_budget')
        finally:
            # An assignment failure leaves only the gated Python child outside
            # the Job Object. It cannot start a core or spawn FFmpeg before the
            # gate is written, and must be killed before releasing its lease.
            if process is not None and not assigned and process.poll() is None:
                process.kill()
            tree.stop_and_close()
            if process is not None:
                process.wait()
        if not response_path.is_file() or response_path.stat().st_size > 16*1024**2:
            raise ModelFailure('processing_response_budget')
        return json.loads(response_path.read_text(encoding='utf8'))
