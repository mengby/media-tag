"""Isolated real CPU execution; no model download or external service."""
from hashlib import sha256
from io import BytesIO
import wave

import pytest

from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.service import AnalysisService
from media_tag.catalog import AccessDenied, Principal
from media_tag.router.runtime import CPUHandlers, LocalExecutor
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.contracts.models import WorkerResult
from media_tag.upload.models import UploadSpec
from .conftest import upload


@pytest.fixture
def intake(intake):
    # Router's generic fixture has an unrelated synthetic.bin registration.
    # Each dispatch case creates the actual media inputs it intends to exercise.
    with intake.database.transaction(write=True) as connection:
        connection.execute('DELETE FROM analysis_intents WHERE asset_id=?', (intake.asset.asset_id,))
    return intake


def service(env, **kwargs):
    return AnalysisService(env.router, AnalysisDeployment(enabled=True, **kwargs))


def test_processing_fast_job_is_independent_and_analysis_really_executes(intake, tools):
    env = intake
    result = upload(env, purpose='processing')
    analysis = service(env)
    assert AnalysisService(env.router).dispatch_pending(principal=env.owner) == []
    dispatched = analysis.dispatch_pending(principal=env.owner)
    assert len(dispatched) == 1 and dispatched[0]['state'] == 'dispatched'
    job_id = dispatched[0]['job_id']
    assert job_id != result['job_id']
    assert [t['stage'] for t in env.router.get_job(result['job_id'], principal=env.owner)['tasks']] == ['probe']
    assert {t['stage'] for t in analysis.status(result['asset_id'], principal=env.owner)['runtime']['tasks']} == {'probe', 'evidence'}
    executor = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], tools),
                             before_acquire=lambda: analysis.dispatch_pending(principal=env.owner))
    for _ in range(8):
        if executor.run_once() is None:
            break
    state = analysis.status(result['asset_id'], principal=env.owner)
    assert state['runtime']['state'] == 'succeeded'
    assert state['state'] == 'awaiting_topic_analysis'
    assert not state['analysis_complete'] and not state['medical_confirmation']
    assert not state['effective_values_written']
    assert analysis.dispatch_pending(principal=env.owner) == []


def test_library_job_is_coalesced_and_ack_loss_cannot_duplicate_it(intake, monkeypatch):
    env = intake
    result = upload(env, purpose='library')
    analysis = service(env)
    submit = env.router.submit
    def lose_ack(*args, **kwargs):
        submit(*args, **kwargs)
        raise RuntimeError('synthetic acknowledgement loss')
    monkeypatch.setattr(env.router, 'submit', lose_ack)
    with pytest.raises(RuntimeError, match='acknowledgement'):
        analysis.dispatch_pending(principal=env.owner)
    monkeypatch.setattr(env.router, 'submit', submit)
    reopened = service(env)
    assert reopened.dispatch_pending(principal=env.owner) == []
    assert reopened.status(result['asset_id'], principal=env.owner)['runtime']['job_id'] == result['job_id']
    env.router.cancel(result['job_id'], principal=env.owner)
    assert reopened.dispatch_pending(principal=env.owner) == []
    assert reopened.status(result['asset_id'], principal=env.owner)['state'] == 'cancelled'


def test_audio_probe_does_not_claim_sound_labels_or_launch_visual_models(intake, tools):
    env = intake
    stream = BytesIO()
    with wave.open(stream, 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\0\0' * 2000)
    data = stream.getvalue()
    session = env.uploads.create(UploadSpec(request_id='synthetic-audio-analysis', filename='synthetic.wav',
        size_bytes=len(data), sha256=sha256(data).hexdigest(), purpose='processing'), principal=env.owner)
    for offset in range(0, len(data), 1024):
        chunk = data[offset:offset+1024]
        env.uploads.append(session['upload_id'], offset, chunk, sha256(chunk).hexdigest(), principal=env.owner)
    result = env.uploads.complete(session['upload_id'], principal=env.owner)
    analysis = service(env)
    analysis.dispatch_pending(principal=env.owner)
    executor = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], tools))
    for _ in range(6):
        if executor.run_once() is None:
            break
    state = analysis.status(result['asset_id'], principal=env.owner)
    assert state['state'] == 'awaiting_audio_provider' and not state['analysis_complete']
    assert {task['stage'] for task in state['runtime']['tasks']} == {'probe'}
    fields = {item['field']: item for item in state['topic_analysis']['fields']}
    assert len(fields) == 10
    assert {field for field, item in fields.items() if item['value']['state'] == 'not_applicable'} == {'画面主体', '表现形式'}
    assert fields['情绪/语气']['next_step'] == 'provider'
    assert not state['topic_analysis']['producers'] and not state['topic_analysis']['analysis_complete']


def test_dispatch_uses_requester_permissions_and_does_not_act_as_another_owner(intake):
    env = intake
    result = upload(env, purpose='processing')
    other = Principal(actor_id='synthetic-other')
    asset = env.catalog.get_asset(result['asset_id'], principal=env.owner)
    env.catalog.change_access(asset.asset_id, principal=env.owner, expected_revision=asset.revision,
        subject_id=other.actor_id, permission='read', reason='synthetic sharing')
    upload(env, request='shared-analysis', principal=other, purpose='processing', known_asset_id=asset.asset_id)
    analysis = service(env)
    assert analysis.dispatch_pending(principal=env.admin) == []
    assert analysis.dispatch_pending(principal=other)[0]['state'] == 'dispatch_unavailable'
    assert analysis.status(asset.asset_id, principal=other)['state'] == 'awaiting_edit_permission'
    with pytest.raises(AccessDenied):
        analysis.dispatch_pending(principal=env.workers[0])
    assert analysis.dispatch_pending(principal=env.owner)[0]['state'] == 'dispatched'
    with env.database.transaction() as c:
        assert c.execute('SELECT COUNT(*) FROM router_requests WHERE owner_id=? AND request_id LIKE ?',
            (other.actor_id, 'analysis-%')).fetchone()[0] == 0


def test_opt_in_config_worker_picks_up_new_intents_while_idle(intake, tools, tmp_path, monkeypatch):
    import media_tag.api.config as module
    env = intake
    monkeypatch.setattr(module, 'load_template_bundle', lambda *a, **k: env.contract)
    config = module.LocalConfig(workspace=str(env.workspace), database=str(env.database.path),
        object_store=str(env.objects), template_dir=str(env.originals),
        template_fingerprint=env.contract.fingerprint,
        credentials={sha256(b'synthetic-worker').hexdigest(): env.workers[0],
                     sha256(b'synthetic-human').hexdigest(): env.owner},
        workers=(env.workers[0].actor_id,), tool_dir=str(tools.ffmpeg.parent),
        analysis=AnalysisDeployment(enabled=True))
    path = tmp_path / 'synthetic-analysis.local.json'
    path.write_text(config.model_dump_json())
    executor = module.executor_from_config(path, env.workers[0].actor_id)
    assert executor.run_once() is None
    result = upload(env, purpose='processing')
    for _ in range(8):
        if executor.run_once() is None:
            break
    state = service(env).status(result['asset_id'], principal=env.owner)
    assert state['runtime']['state'] == 'succeeded' and not state['analysis_complete']


def test_bounded_scan_does_not_let_blocked_record_starve_later_intents(intake, monkeypatch):
    env = intake
    first = upload(env, request='blocked', purpose='processing')
    second = upload(env, request='later', purpose='processing')
    assert first['asset_id'] != second['asset_id']
    analysis = service(env, dispatch_limit=1)
    monkeypatch.setattr(analysis.files, '_gate', lambda connection, asset:
        'privacy_review_pending' if asset['asset_id'] == first['asset_id'] else None)
    assert analysis.dispatch_pending(principal=env.owner)[0]['state'] == 'awaiting_file_gate'
    assert analysis.dispatch_pending(principal=env.owner)[0]['state'] == 'dispatched'
    assert analysis.status(second['asset_id'], principal=env.owner)['runtime_dispatched']
    assert analysis.dispatch_pending(principal=env.owner)[0]['state'] == 'awaiting_file_gate'


def register_unknown_image(env):
    # Misleading filename deliberately disagrees with the real PNG bytes.
    path = env.originals / 'synthetic-not-audio.wav'
    path.write_bytes(env.data)
    asset = env.catalog.register_file(FilesystemBackend(env.originals), path.name,
        source_backend_id='synthetic-direct', principal=env.owner,
        request_id='direct-unknown-media', name='synthetic direct media')
    return asset, path


def test_direct_registration_probes_actual_bytes_and_preserves_probe_after_replan_ack_loss(intake, tools, monkeypatch):
    env = intake
    asset, source = register_unknown_image(env)
    original = sha256(source.read_bytes()).hexdigest()
    analysis = service(env)
    job_id = analysis.dispatch_pending(principal=env.owner)[0]['job_id']
    assert analysis.status(asset.asset_id, principal=env.owner)['content_kind'] is None
    job = env.router.get_job(job_id, principal=env.owner)
    probe_id = job['tasks'][0]['task_id']
    executor = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], tools))
    assert executor.run_once()['accepted']
    assert analysis.status(asset.asset_id, principal=env.owner)['state'] == 'awaiting_probe_adoption'
    replan = env.router.replan
    def lose_replan_ack(*args, **kwargs):
        replan(*args, **kwargs)
        raise RuntimeError('synthetic replan acknowledgement loss')
    monkeypatch.setattr(env.router, 'replan', lose_replan_ack)
    with pytest.raises(RuntimeError, match='acknowledgement'):
        analysis.dispatch_pending(principal=env.owner)
    monkeypatch.setattr(env.router, 'replan', replan)
    reopened = service(env)
    assert reopened.dispatch_pending(principal=env.owner)[0]['state'] == 'probe_adopted'
    state = reopened.status(asset.asset_id, principal=env.owner)
    assert state['content_kind'] == 'image' and state['runtime']['revision'] == 2
    assert executor.run_once()['accepted'] and executor.run_once() is None
    probe = next(t for t in env.router.get_job(job_id, principal=env.owner)['tasks'] if t['stage'] == 'probe')
    assert probe['task_id'] == probe_id and probe['attempt'] == 1
    assert sha256(source.read_bytes()).hexdigest() == original
    assert not reopened.status(asset.asset_id, principal=env.owner)['analysis_complete']


def test_corrupt_probe_report_is_not_used_to_classify_or_expand_job(intake, tools):
    env = intake
    asset, _ = register_unknown_image(env)
    analysis = service(env)
    job_id = analysis.dispatch_pending(principal=env.owner)[0]['job_id']
    executor = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], tools))
    assert executor.run_once()['accepted']
    with env.database.transaction() as connection:
        row = connection.execute('''SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id)
            WHERE job_id=? AND active=1''', (job_id,)).fetchone()
    result = WorkerResult.model_validate_json(row['result_json'])
    (env.workspace / result.artifacts[0].ref).write_text('{"kind":"audio"}')
    assert analysis.dispatch_pending(principal=env.owner)[0]['state'] == 'dispatch_unavailable'
    state = analysis.status(asset.asset_id, principal=env.owner)
    assert state['content_kind'] is None and not state['analysis_complete']
    assert len(state['runtime']['tasks']) == 1


def test_unknown_cancelled_job_is_not_expanded_or_restarted(intake, tools):
    env = intake
    asset, _ = register_unknown_image(env)
    analysis = service(env)
    job_id = analysis.dispatch_pending(principal=env.owner)[0]['job_id']
    executor = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], tools))
    executor.run_once()
    env.router.cancel(job_id, principal=env.owner)
    assert service(env).dispatch_pending(principal=env.owner)[0]['state'] == 'cancelled'
    state = analysis.status(asset.asset_id, principal=env.owner)
    assert state['content_kind'] is None and len(state['runtime']['tasks']) == 1


def test_invalid_registered_bytes_keep_probe_failure_without_false_type_or_retry_loop(intake, tools):
    env = intake
    env.data = b'synthetic invalid media, not audio or image'
    asset, _ = register_unknown_image(env)
    analysis = service(env)
    analysis.dispatch_pending(principal=env.owner)
    executor = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], tools))
    assert executor.run_once()['accepted']
    for _ in range(3):
        outcome = analysis.dispatch_pending(principal=env.owner)
        assert outcome[0]['state'] in ('failed', 'needs_attention')
        assert executor.run_once() is None
    state = analysis.status(asset.asset_id, principal=env.owner)
    assert state['content_kind'] is None and not state['analysis_complete']
    assert state['runtime']['tasks'][0]['attempt'] == 1
