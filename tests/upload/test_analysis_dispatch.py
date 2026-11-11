"""Isolated real CPU execution; no model download or external service."""
from hashlib import sha256
from io import BytesIO
import wave

import pytest

from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.service import AnalysisService
from media_tag.catalog import AccessDenied, Principal
from media_tag.router.runtime import CPUHandlers, LocalExecutor
from media_tag.upload.models import UploadSpec
from .conftest import upload


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
    assert analysis.dispatch_pending(principal=env.owner) == []
    assert analysis.dispatch_pending(principal=env.owner)[0]['state'] == 'awaiting_file_gate'
