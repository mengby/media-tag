"""Explicit analysis repair on isolated real CPU tasks, without new model calls."""
from hashlib import sha256
import json

import pytest
from fastapi.testclient import TestClient

from media_tag.analysis.repair import RepairSubmission, repair
from media_tag.api.app import create_app
from media_tag.catalog import Principal, RevisionConflict
from media_tag.contracts.models import ContractModel
from media_tag.router.models import StageDefinition
from media_tag.router.runtime import CPUHandlers, LocalExecutor

from .conftest import upload
from .test_analysis_dispatch import intake, service


def prepare(env, tools, *, finish=True):
    result = upload(env, purpose='processing')
    analysis = service(env)
    analysis.dispatch_pending(principal=env.owner)
    executor = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], tools))
    if finish:
        for _ in range(8):
            if executor.run_once() is None:
                break
    return result['asset_id'], analysis, executor


def submission(env, analysis, asset_id, **changes):
    asset = env.catalog.get_asset(asset_id, principal=env.owner)
    state = analysis.status(asset_id, principal=env.owner)
    return RepairSubmission(request_id='synthetic-repair', source_sha256=asset.sha256,
        expected_asset_revision=asset.revision, expected_job_revision=state['runtime']['revision'], **changes)


def task_rows(env, analysis, asset_id):
    job_id = analysis.status(asset_id, principal=env.owner)['runtime']['job_id']
    with env.database.transaction() as connection:
        return {row['step_key']: dict(row) for row in connection.execute('''SELECT t.*,a.result_json
            FROM router_tasks t LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE job_id=? AND active=1''', (job_id,))}


def corrupt(env, rows, key):
    result = json.loads(rows[key]['result_json'])
    (env.workspace / result['artifacts'][0]['ref']).write_bytes(b'synthetic damaged report')


def test_valid_stages_are_reused_and_receipt_survives_reopen_and_old_revision_retry(intake, tools):
    env = intake
    asset_id, analysis, _ = prepare(env, tools)
    spec = submission(env, analysis, asset_id)
    before = task_rows(env, analysis, asset_id)
    result = repair(analysis, asset_id, spec, principal=env.owner)
    assert result['patched_roots'] == [] and set(result['stage_assessments'].values()) == {'valid'}
    assert result['reused_stage_keys'] == ['evidence', 'probe'] and not result['replanned_keys']
    assert len(result['pending_capability_fields']) == 10 and not result['analysis_complete']
    assert task_rows(env, analysis, asset_id) == before
    assert repair(service(env), asset_id, spec, principal=env.owner)['reused_request']
    with env.database.transaction() as connection:
        assert connection.execute("SELECT COUNT(*) FROM router_events WHERE action='analysis_repair'").fetchone()[0] == 1
    with pytest.raises(RevisionConflict):
        repair(analysis, asset_id, spec.model_copy(update={'fields': ('科室',)}), principal=env.owner)


@pytest.mark.parametrize('damaged', ['probe', 'evidence'])
def test_only_invalid_stage_and_its_descendants_get_new_generations(intake, tools, damaged):
    env = intake
    asset_id, analysis, executor = prepare(env, tools)
    before_asset = env.catalog.get_asset(asset_id, principal=env.owner)
    before = task_rows(env, analysis, asset_id)
    corrupt(env, before, damaged)
    spec = submission(env, analysis, asset_id)
    result = repair(analysis, asset_id, spec, principal=env.owner)
    assert result['patched_roots'] == [damaged]
    assert result['replanned_keys'] == (['evidence'] if damaged == 'evidence' else ['evidence', 'probe'])
    after = task_rows(env, analysis, asset_id)
    assert after['evidence']['task_id'] != before['evidence']['task_id']
    assert (after['probe']['task_id'] == before['probe']['task_id']) == (damaged == 'evidence')
    assert repair(service(env), asset_id, spec, principal=env.owner)['reused_request']
    for _ in range(6):
        if executor.run_once() is None:
            break
    assert analysis.status(asset_id, principal=env.owner)['runtime']['state'] == 'succeeded'
    current = env.catalog.get_asset(asset_id, principal=env.owner)
    assert before_asset.revision == current.revision and before_asset.effective_values == current.effective_values


def test_replan_and_receipt_roll_back_together_if_audit_persistence_fails(intake, tools, monkeypatch):
    env = intake
    asset_id, analysis, _ = prepare(env, tools)
    before = task_rows(env, analysis, asset_id)
    corrupt(env, before, 'evidence')
    spec = submission(env, analysis, asset_id)
    event = env.router._event
    def fail_receipt(connection, job_id, task_id, actor, action, detail):
        if action == 'analysis_repair':
            raise RuntimeError('synthetic receipt failure')
        return event(connection, job_id, task_id, actor, action, detail)
    monkeypatch.setattr(env.router, '_event', fail_receipt)
    with pytest.raises(RuntimeError, match='receipt failure'):
        repair(analysis, asset_id, spec, principal=env.owner)
    assert task_rows(env, analysis, asset_id) == before
    assert analysis.status(asset_id, principal=env.owner)['runtime']['revision'] == spec.expected_job_revision
    monkeypatch.setattr(env.router, '_event', event)
    assert repair(analysis, asset_id, spec, principal=env.owner)['patched_roots'] == ['evidence']


def test_live_stages_are_not_restarted_and_new_configured_stage_can_be_added(intake, tools):
    env = intake
    asset_id, analysis, executor = prepare(env, tools, finish=False)
    before = task_rows(env, analysis, asset_id)
    result = repair(analysis, asset_id, submission(env, analysis, asset_id), principal=env.owner)
    assert result['patched_roots'] == [] and set(result['stage_assessments'].values()) == {'pending'}
    assert task_rows(env, analysis, asset_id) == before
    for _ in range(8):
        if executor.run_once() is None:
            break
    # A synthetic capability declaration tests missing-stage dispatch only, not inference.
    env.router.stages['visual_fields'] = StageDefinition(ContractModel, 'synthetic-vlm-adapter',
        required_model_revision='synthetic-model', required_preprocess_version='synthetic-visual',
        required_gpu_slots=0,
        allowed_target_fields=('ai_text',))
    spec = submission(env, analysis, asset_id)
    spec = spec.model_copy(update={'request_id': 'new-configured-field'})
    result = repair(analysis, asset_id, spec, principal=env.owner)
    assert result['patched_roots'] == ['visual_fields']
    assert result['stage_assessments']['probe'] == result['stage_assessments']['evidence'] == 'valid'
    assert task_rows(env, analysis, asset_id)['probe']['task_id'] == before['probe']['task_id']


def test_http_repair_requires_owner_scope_cas_and_body_validation(intake, tools):
    env = intake
    asset_id, analysis, _ = prepare(env, tools)
    app = create_app(env.router, {sha256(name.encode()).hexdigest(): person for name, person in
        [('owner', env.owner), ('worker', env.workers[0]), ('other', Principal(actor_id='synthetic-other')),
         ('reader', Principal(actor_id=env.owner.actor_id, role='reader'))]}, analysis_options=analysis.options)
    client = TestClient(app)
    path = '/analysis/assets/'+asset_id+'/repair'
    spec = submission(env, analysis, asset_id, fields=('科室',))
    payload = spec.model_dump(mode='json')
    assert client.post(path, json=payload).status_code == 401
    assert client.post(path, json=payload, headers={'Authorization': 'Bearer worker'}).status_code == 403
    assert client.post(path, json=payload, headers={'Authorization': 'Bearer other'}).status_code == 403
    assert client.post(path, json=payload, headers={'Authorization': 'Bearer reader'}).status_code == 403
    headers = {'Authorization': 'Bearer owner'}
    assert client.post(path, json=payload | {'expected_job_revision': 999}, headers=headers).status_code == 409
    assert client.post(path, json=payload | {'source_sha256': 'a'*64}, headers=headers).status_code == 409
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 200 and response.json()['pending_capability_fields'] == ['科室']
    assert not response.json()['patched_roots']
    assert client.post(path, json=payload, headers=headers).json()['reused_request']
    assert client.post(path, json=payload | {'fields': ['invalid-field']}, headers=headers).status_code == 422


def test_paused_cancelled_or_disabled_jobs_cannot_be_repaired(intake, tools):
    env = intake
    asset_id, analysis, _ = prepare(env, tools)
    spec = submission(env, analysis, asset_id)
    analysis.options = analysis.options.model_copy(update={'enabled': False})
    with pytest.raises(RevisionConflict, match='disabled'):
        repair(analysis, asset_id, spec, principal=env.owner)
    analysis.options = analysis.options.model_copy(update={'enabled': True})
    job_id = analysis.status(asset_id, principal=env.owner)['runtime']['job_id']
    env.router.pause(job_id, principal=env.owner)
    with pytest.raises(RevisionConflict, match='paused'):
        repair(analysis, asset_id, spec, principal=env.owner)
    env.router.pause(job_id, principal=env.owner, paused=False)
    env.router.cancel(job_id, principal=env.owner)
    with pytest.raises(RevisionConflict, match='cancelled'):
        repair(analysis, asset_id, spec, principal=env.owner)


def test_changed_preprocessing_configuration_rebuilds_only_the_incompatible_dependency_tree(intake, tools):
    env = intake
    asset_id, analysis, executor = prepare(env, tools)
    before = task_rows(env, analysis, asset_id)
    analysis.options = analysis.options.model_copy(update={'probe_max_pixels': 8*1024**2})
    result = repair(analysis, asset_id, submission(env, analysis, asset_id), principal=env.owner)
    assert result['stage_assessments'] == {'evidence': 'valid', 'probe': 'incompatible'}
    assert result['patched_roots'] == ['probe']
    after = task_rows(env, analysis, asset_id)
    assert all(after[key]['task_id'] != before[key]['task_id'] for key in before)
    assert executor.run_once()['accepted'] and executor.run_once()['accepted']
    assert analysis.status(asset_id, principal=env.owner)['runtime']['state'] == 'succeeded'


def test_artifact_verification_does_not_hold_sqlite_writer_lock(intake, tools, monkeypatch):
    env = intake
    asset_id, analysis, _ = prepare(env, tools)
    original = env.router._artifacts
    writes = []
    def checked(task, result):
        with env.database.transaction(write=True) as connection:
            connection.execute("UPDATE catalog_metadata SET value=value WHERE key='template_fingerprint'")
            writes.append(task.stage)
        return original(task, result)
    monkeypatch.setattr(env.router, '_artifacts', checked)
    assert repair(analysis, asset_id, submission(env, analysis, asset_id), principal=env.owner)['patched_roots'] == []
    assert set(writes) == {'probe', 'evidence'}
