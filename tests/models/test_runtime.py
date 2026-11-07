from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import subprocess
import sys
import threading

import pytest

from media_tag.contracts.models import EffectiveValue, FieldValue, TaskLimits, Versions, WorkerResult
from media_tag.models.contracts import ModelFailure
from media_tag.models.runtime import ModelHandlers, PREPROCESS_VERSION, model_stages
from media_tag.router.models import JobSpec, StepSpec
from media_tag.router.runtime import CPUHandlers, LocalExecutor
from upload.conftest import upload


def configure(env, registry):
    env.router.stages.update(model_stages(registry))
    for worker in env.workers:
        env.router.register_worker(worker.actor_id, tuple(env.router.stages), principal=env.admin)


def step(registry, kind='ocr', **kwargs):
    values = dict(key=kind, stage=kind, versions=Versions(rules_version='synthetic-v1', model_revision=registry.revision, preprocess_version=PREPROCESS_VERSION),
                  limits=TaskLimits(gpu_slots=int(kind == 'clip')))
    values.update(kwargs)
    return StepSpec(**values)


@pytest.mark.parametrize('fault', ['revision', 'preprocess', 'gpu', 'target'])
def test_model_versions_gpu_and_target_fields_are_bound_at_submission(intake, registry, fault):
    env = intake; configure(env, registry)
    task = step(registry, 'clip')
    if fault == 'revision':
        task = task.model_copy(update={'versions': task.versions.model_copy(update={'model_revision': 'changed'})})
    elif fault == 'preprocess':
        task = task.model_copy(update={'versions': task.versions.model_copy(update={'preprocess_version': 'changed'})})
    elif fault == 'gpu':
        task = task.model_copy(update={'limits': TaskLimits(gpu_slots=0)})
    else:
        task = task.model_copy(update={'target_fields': ('medical',)})
    with pytest.raises(ValueError):
        env.router.submit(JobSpec(asset_id=env.asset.asset_id, steps=(task,)), request_id='invalid-model', principal=env.owner)


def test_queued_revision_is_revalidated_before_claim(intake, registry):
    env = intake; configure(env, registry)
    job = env.router.submit(JobSpec(asset_id=env.asset.asset_id, steps=(step(registry),)), request_id='old-model', principal=env.owner)
    env.router.stages['ocr'] = replace(env.router.stages['ocr'], required_model_revision='changed')
    assert env.router.claim(principal=env.workers[0]) is None
    status = env.router.get_job(job['job_id'], principal=env.owner)
    assert status['state'] == 'needs_attention' and not status['resources']


def test_actual_upload_runs_five_stages_and_preserves_human_values(intake, registry, tools, text_image):
    env = intake; configure(env, registry)
    env.uploads.options = env.uploads.options.model_copy(update={'chunk_bytes': 65536})
    image = BytesIO(); text_image.save(image, 'PNG'); env.data = image.getvalue()
    uploaded = upload(env)
    asset_id = uploaded['asset_id']
    before = env.catalog.revise(asset_id, principal=env.owner, expected_revision=1, reason='synthetic human revision',
        values=(EffectiveValue(field='ai_text', value=FieldValue(state='observed', values=('synthetic human text',)),
            actor_id=env.owner.actor_id, revision=2, evidence_ids=('synthetic-human-evidence',), confirmed_at=datetime.now(timezone.utc)),))
    cpu = CPUHandlers(env.router, env.workers[0], tools)
    models = ModelHandlers(env.router, env.workers[0], registry)
    executor = LocalExecutor(env.router, env.workers[0], lambda lease, stopped: cpu(lease, stopped) if lease.task.stage in ('probe', 'evidence') else models(lease, stopped))
    outcomes = [executor.run_once() for _ in range(5)]
    assert all(outcome and outcome['accepted'] for outcome in outcomes)
    assert executor.run_once() is None
    status = env.router.get_job(uploaded['job_id'], principal=env.owner)
    assert status['state'] == 'needs_attention' and not status['resources']
    with env.database.transaction() as connection:
        rows = connection.execute('SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?', (uploaded['job_id'],)).fetchall()
    reports = {}
    # Stage is carried in task spec rather than a free form native output.
    for row in rows:
        result = WorkerResult.model_validate_json(row['result_json'])
        for ref in result.artifacts:
            if ref.kind == 'model_report':
                report = json.loads((env.workspace / ref.ref).read_text())
                reports[report['kind']] = report
                assert result.evidence and not result.suggestions
                assert report['source_sha256'] == sha256(env.data).hexdigest()
                assert not report['effective_values_written'] and not report['full_video_observed']
                assert report['elapsed_seconds'] > 0 and report['peak_process_bytes'] > 0
                assert result.status == ('partial' if report['kind'] == 'clip' else 'succeeded')
    assert set(reports) == {'ocr', 'face', 'clip'}
    assert 'ABC' in ''.join(v['text'] for v in reports['ocr']['frames'][0]['observation']['lines'])
    assert len(reports['clip']['frames'][0]['observation']['embedding']) == 512
    assert reports['clip']['candidate_state'] == 'missing_published_vocabulary'
    assert env.catalog.get_asset(asset_id, principal=env.owner) == before
    # A processing-only upload remains independent of semantic analysis.
    processing = upload(env, request='processing-only', purpose='processing')
    assert len(env.router.get_job(processing['job_id'], principal=env.owner)['tasks']) == 1


@pytest.fixture
def prepared(intake, registry, tools):
    env = intake; configure(env, registry)
    uploaded = upload(env)
    executor = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], tools))
    assert executor.run_once()['accepted'] and executor.run_once()['accepted']
    lease = env.router.claim(principal=env.workers[0])
    return env, lease, ModelHandlers(env.router, env.workers[0], registry)


@pytest.mark.parametrize('fault', ['cancelled', 'model_timeout'])
def test_real_subprocess_exits_before_handler_releases_control(prepared, monkeypatch, fault):
    env, lease, handler = prepared
    real_popen = subprocess.Popen; processes = []; stopped = threading.Event()
    def slow(*args, **kwargs):
        process = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
        processes.append(process)
        if fault == 'cancelled':
            stopped.set()
        return process
    monkeypatch.setattr('media_tag.models.runtime.subprocess.Popen', slow)
    lease = lease.model_copy(update={'task': lease.task.model_copy(update={'limits': TaskLimits(timeout_seconds=1, gpu_slots=lease.task.limits.gpu_slots)})})
    with pytest.raises(ModelFailure, match=fault):
        handler(lease, stopped)
    assert len(processes) == 1 and processes[0].poll() is not None
    env.router.release(lease.task.task_id, lease.task.lease_epoch, principal=env.workers[0])


def test_tampered_frame_fails_without_business_writes(prepared):
    env, lease, handler = prepared
    frame = next(ref for ref in lease.task.input_refs if ref.kind == 'image')
    (env.workspace / frame.ref).write_bytes(b'tampered')
    with pytest.raises(ModelFailure, match='frame_integrity'):
        handler(lease, threading.Event())


def test_missing_visual_dependency_is_not_silently_inferred(prepared):
    env, lease, handler = prepared
    lease = lease.model_copy(update={'task': lease.task.model_copy(update={'input_refs': (lease.task.input_refs[0],)})})
    with pytest.raises(ModelFailure, match='missing_evidence_manifest'):
        handler(lease, threading.Event())


@pytest.mark.parametrize('fault', ['oom', 'missing_dependency'])
def test_runner_records_safe_explicit_failures(tmp_path, monkeypatch, fault):
    from media_tag.models import runner
    class OutOfMemoryError(RuntimeError):
        pass
    def fail(request):
        raise OutOfMemoryError('synthetic private diagnostic') if fault == 'oom' else ModuleNotFoundError('synthetic missing dependency')
    monkeypatch.setattr(runner, 'execute', fail)
    request, response = tmp_path / 'request.json', tmp_path / 'response.json'
    request.write_text('{}')
    assert runner.main([str(request), str(response)]) == 2
    assert json.loads(response.read_text()) == {'ok': False, 'error_code': 'oom' if fault == 'oom' else 'model_execution_failed'}


def test_actual_audio_model_stages_are_not_applicable(intake, registry, tools):
    env = intake; configure(env, registry)
    path = env.originals / 'synthetic.wav'
    tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '0.1', '-y', str(path)], timeout=10)
    # Register the actual WAV through upload; extension must agree with decoded format.
    from upload.conftest import append, spec
    env.data = path.read_bytes(); env.uploads.options = env.uploads.options.model_copy(update={'chunk_bytes': 65536})
    session = env.uploads.create(spec(env).model_copy(update={'filename': 'synthetic.wav'}), principal=env.owner)
    append(env, session)
    uploaded = env.uploads.complete(session['upload_id'], principal=env.owner)
    cpu = CPUHandlers(env.router, env.workers[0], tools); models = ModelHandlers(env.router, env.workers[0], registry)
    executor = LocalExecutor(env.router, env.workers[0], lambda lease, stopped: cpu(lease, stopped) if lease.task.stage in ('probe', 'evidence') else models(lease, stopped))
    assert all(executor.run_once()['accepted'] for _ in range(5))
    status = env.router.get_job(uploaded['job_id'], principal=env.owner)
    assert status['state'] == 'succeeded' and not status['resources']
    with env.database.transaction() as connection:
        results = [WorkerResult.model_validate_json(row[0]) for row in connection.execute(
            'SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?', (uploaded['job_id'],))]
    assert sum(result.status == 'not_applicable' for result in results) == 4
