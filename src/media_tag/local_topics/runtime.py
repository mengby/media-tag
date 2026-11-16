from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.analysis.fields import rules_digest, topic_names
from media_tag.contracts.models import ArtifactRef, TagSuggestion, TaskLimits, Versions
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import StageDefinition, StepSpec
from media_tag.semantic.registry import SemanticRegistry
from media_tag.storage.filesystem import FilesystemBackend, require_local_path
from media_tag.transforms.processes import ProcessTree
from media_tag.workers.evidence import _directory

from .contracts import Generation, LocalTopicOptions, RouterTopicReport
from .batching import TopicBatch, cache_key
from .prompt import PROMPT_VERSION
from .parser import protocol_fields
from .service import LocalTopicService, PREPROCESS_VERSION

VERSION = 'ct-local-topics-router-v2'


def stage(registry):
    return StageDefinition(LocalTopicOptions, VERSION+':'+registry.revision+':'+PROMPT_VERSION,
        gpu_allowed=True, metadata_sensitive=False, required_model_revision=registry.revision,
        required_preprocess_version=PREPROCESS_VERSION, required_gpu_slots=1, allowed_target_fields=TOPIC_FIELDS)


def steps_for_fields(router, *, include_original=False):
    policy = router.catalog.review_policy
    revision = router.stages['local_topics'].required_model_revision
    groups = [('local_topics', TOPIC_FIELDS)] if include_original else []
    groups.extend(('local-field-' + sha256(name.encode()).hexdigest()[:24], (name,))
                  for name in policy.tag_fields.guidance)
    steps = []
    for key, fields in groups:
        digest = rules_digest(policy, fields)
        options = LocalTopicOptions(model_revision=revision, policy_digest=digest)
        steps.append(StepSpec(key=key, stage='local_topics', depends_on=('probe',), target_fields=fields,
            config=options.model_dump(mode='json'), prompt_version=PROMPT_VERSION,
            versions=Versions(rules_version='sha256:'+digest, preprocess_version=PREPROCESS_VERSION,
                              taxonomy_version=policy.config.taxonomy_version, model_revision=revision),
            limits=TaskLimits(gpu_slots=1, timeout_seconds=min(240, router.options.max_timeout_seconds))))
    return tuple(steps)


def suggestions(document, task):
    return tuple(TagSuggestion(suggestion_id='local-topic-'+sha256(
        f'{task.task_id}:{task.lease_epoch}:{item.field}'.encode()).hexdigest()[:32], field=item.field,
        value=item.value, origin='ai', producer_version=PROMPT_VERSION, evidence_ids=item.evidence_ids)
        for item in document.fields if item.field in task.target_fields)


def read_json(router, artifact):
    with router.files.open_read(artifact.ref) as stream:
        raw = stream.read(1024*1024+1)
    if len(raw) > 1024*1024 or sha256(raw).hexdigest() != artifact.sha256:
        raise ValueError('local_topic_report_integrity')
    return json.loads(raw)


class LocalTopicHandlers:
    def __init__(self, router, worker, deployment):
        self.router, self.worker, self.deployment = router, worker, deployment
        self.registry = SemanticRegistry(Path(deployment.model_root))
        self.batch = TopicBatch(router, deployment.max_batch_items if deployment.batch_size > 1 else 1)

    def __call__(self, lease, stopped):
        task = lease.task
        deadline = time.monotonic()+task.limits.timeout_seconds-min(2., task.limits.timeout_seconds*.25)
        def check():
            if stopped.is_set():raise ModelFailure('cancelled')
            if time.monotonic() >= deadline:raise ModelFailure('local_topic_timeout')
        check()
        options = LocalTopicOptions.model_validate_json(json.dumps(lease.config))
        policy = getattr(self.router.catalog, 'review_policy', None)
        with self.router.database.transaction() as connection:
            row = connection.execute('SELECT spec_json FROM router_tasks WHERE task_id=? AND lease_epoch=? AND active=1',
                (task.task_id,task.lease_epoch)).fetchone()
        spec = StepSpec.model_validate_json(row['spec_json']) if row else None
        if (not self.deployment.enabled or policy is None or options.policy_digest != rules_digest(policy, task.target_fields)
                or spec is None or spec.prompt_version != PROMPT_VERSION or spec.target_fields != task.target_fields
                or options.model_revision != self.registry.revision or task.versions.model_revision != self.registry.revision
                or task.versions.rules_version != 'sha256:'+options.policy_digest
                or task.versions.taxonomy_version != policy.config.taxonomy_version
                or task.versions.preprocess_version != PREPROCESS_VERSION or task.limits.gpu_slots != 1
                or not task.target_fields or not set(task.target_fields) <= set(topic_names(policy))):
            raise ModelFailure('local_topic_configuration_changed')
        output = _directory(self.router.workspace, task.output_prefix.rstrip('/'))
        source_dir = _directory(output, 'source')
        drafts = _directory(output, 'drafts')
        source_path = source_dir/'input.bin'
        digest, count = sha256(), 0
        with self.router.open_input(task.task_id, task.lease_epoch, principal=self.worker) as source, source_path.open('xb') as target:
            while chunk := source.read(1024**2):
                check();count += len(chunk)
                if count > options.max_input_bytes:raise ModelFailure('local_topic_input_budget')
                digest.update(chunk);target.write(chunk)
        if digest.hexdigest() != task.input_refs[0].sha256:
            raise ModelFailure('local_topic_source_integrity')
        native_path = output/'model-input.png'
        fields = task.target_fields if set(task.target_fields).difference(TOPIC_FIELDS) else TOPIC_FIELDS
        owner = self
        key = cache_key(task.task_id, task.lease_epoch, digest.hexdigest(), spec, task.input_refs[1:])
        batch_id, batch_size, cache_hit = f'{task.task_id}:{task.lease_epoch}', 1, False
        class Generator:
            model_revision = options.model_revision
            def __call__(self, *, image_bytes, field_prompts, options):
                nonlocal batch_id, batch_size, cache_hit
                native_path.write_bytes(image_bytes)
                image_sha = sha256(image_bytes).hexdigest()
                cached = owner.batch.cache.pop(key, None)
                if cached and cached[0].image_sha256 == image_sha:
                    generation, batch_id, batch_size = cached
                    cache_hit = True
                    return generation
                request = {'model_root': str(owner.registry.root), 'model_revision': self.model_revision,
                    'image_path': str(native_path), 'image_sha256': image_sha,
                    'field_prompts': field_prompts, 'options': options.model_dump(mode='json'),
                    'protocol_fields': protocol_fields(policy, fields), 'microbatch_size': owner.deployment.batch_size,
                    'deadline': deadline, 'prompt_version': PROMPT_VERSION, 'start_gate': str(output/'start.ready')}
                check()
                with tempfile.TemporaryDirectory(dir=output, prefix='batch-') as temp:
                    candidates = owner.batch.candidates(task, spec, options, Path(temp), check)
                    check()
                    if not candidates:
                        return owner.invoke(request, output, task, stopped, deadline=deadline)
                    request['prefetch'] = [item for _, item in candidates]
                    try:
                        primary, others = owner.invoke(request, output, task, stopped, deadline=deadline)
                    except ModelFailure as exc:
                        if exc.code not in ('local_topic_model_failed', 'local_topic_process_failed'):
                            raise
                        check()
                        retry = _directory(output, 'single-retry')
                        request.pop('prefetch')
                        request['start_gate'] = str(retry/'start.ready')
                        return owner.invoke(request, retry, task, stopped, deadline=deadline)
                    batch_size = 1 + len(others)
                    for (member_key, member), generation in zip(candidates[:len(others)], others, strict=True):
                        if (generation is not None and generation.image_sha256 == member['image_sha256']
                                and generation.backend == 'local_qwen'
                                and generation.model_revision == self.model_revision
                                and generation.prompt_version == PROMPT_VERSION
                                and len(generation.answers) == len(field_prompts)):
                            owner.batch.remember(member_key, generation, batch_id, batch_size)
                    return primary
        result = LocalTopicService(drafts, policy=policy, generator=Generator(), options=options,
            artifact_root=self.router.workspace).run(FilesystemBackend(source_dir), source_path.name,
            expected_source_sha256=digest.hexdigest(), asset_id=task.asset_id,
            asset_revision=task.asset_revision,
            fields=fields,
            analysis_id='local-topic-'+sha256(
                f'{task.task_id}:{task.lease_epoch}'.encode()).hexdigest())
        check()
        if result.backend != 'local_qwen':raise ModelFailure('local_topic_synthetic_result_rejected')
        source_ref = ArtifactRef(kind='local_topic_source', ref=source_path.relative_to(self.router.workspace).as_posix(), sha256=digest.hexdigest())
        native_ref = ArtifactRef(kind='local_topic_native_input', ref=native_path.relative_to(self.router.workspace).as_posix(), sha256=result.image.sha256)
        document = result.document.model_copy(update={'fields':tuple(item.model_copy(update={'versions':task.versions}) if item.versions else item
                                                                   for item in result.document.fields)})
        result = result.model_copy(update={'document':document})
        report = RouterTopicReport(task_id=task.task_id, lease_epoch=task.lease_epoch,
            input_fingerprint=task.input_fingerprint, versions=task.versions,
            source=source_ref, native_input=native_ref, result=result, inference_batch_id=batch_id,
            inference_batch_size=batch_size, batch_cache_hit=cache_hit)
        path = output/'local-topics.json';path.write_text(report.model_dump_json(), encoding='utf8')
        report_ref = ArtifactRef(kind='local_topic_report', ref=path.relative_to(self.router.workspace).as_posix(), sha256=sha256(path.read_bytes()).hexdigest())
        return {'status':'succeeded', 'artifacts':(report_ref, source_ref, native_ref, result.image, result.raw_report),
                'evidence':document.evidence, 'suggestions':suggestions(document, task)}

    def invoke(self, request, output, task, stopped, *, deadline):
        request_path, response_path = output/'request.json', output/'response.json'
        if (output/'start.ready').exists() or response_path.exists():
            raise ModelFailure('local_topic_attempt_output_exists')
        encoded = json.dumps(request, ensure_ascii=False)
        if len(encoded.encode()) > 512*1024:raise ModelFailure('local_topic_request_budget')
        request_path.write_text(encoded, encoding='utf8')
        # Restrict inherited environment: no service credentials, user paths or source names.
        names = ('SYSTEMROOT','WINDIR','PATH','TEMP','TMP','VIRTUAL_ENV','PYTHONPATH')
        env = {key:value for key,value in os.environ.items() if key.upper() in names}
        env.update(HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
            TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='1', PYTHONUTF8='1')
        tree, process = ProcessTree(), None
        can_yield = len(request.get('prefetch', ())) >= request.get('microbatch_size', 4)
        next_poll = time.monotonic()
        try:
            with (output/'native.stdout.log').open('wb') as stdout, (output/'native.stderr.log').open('wb') as stderr:
                process = subprocess.Popen([sys.executable,'-m','media_tag.local_topics.runner',str(request_path),str(response_path)],
                    env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                    creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                tree.assign(process)
                (output/'start.ready').write_text('assigned\n')
                while process.poll() is None:
                    if stopped.wait(.05):raise ModelFailure('cancelled')
                    if time.monotonic() >= deadline:raise ModelFailure('local_topic_timeout')
                    if can_yield and time.monotonic() >= next_poll:
                        if self.batch.should_yield(task.job_id):
                            (output/'finish-batch').touch()
                            can_yield = False
                        next_poll = time.monotonic() + 2.
                    if os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size > 2*1024**2:
                        raise ModelFailure('local_topic_log_budget')
                if os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size > 2*1024**2:
                    raise ModelFailure('local_topic_log_budget')
        finally:
            tree.stop_and_close()
            if process is not None:
                if process.poll() is None:process.kill()
                process.wait()
        member_count = len(request.get('prefetch', ()))
        if not response_path.is_file() or response_path.stat().st_size > (member_count + 1)*256*1024:
            raise ModelFailure('local_topic_process_failed')
        if process.returncode != 0:raise ModelFailure('local_topic_process_failed')
        response = json.loads(response_path.read_text(encoding='utf8'))
        if response.get('ok') is not True or set(response) != {'ok','generation','prefetched'}:
            raise ModelFailure('local_topic_model_failed')
        if not isinstance(response['prefetched'], list) or len(response['prefetched']) > member_count:
            raise ModelFailure('local_topic_model_failed')
        primary = Generation.model_validate_json(json.dumps(response['generation']))
        if not member_count:
            return primary
        others = []
        for item in response['prefetched']:
            try:
                others.append(Generation.model_validate_json(json.dumps(item)))
            except ValueError:
                others.append(None)
        return primary, others
