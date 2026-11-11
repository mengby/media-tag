"""Real Router/result validation with a synthetic VLM protocol, never model quality proof."""
import json
import threading

import pytest
from fastapi.testclient import TestClient

from media_tag.analysis.intake import ensure_intent
from media_tag.analysis.service import AnalysisService
from media_tag.catalog import Principal
from media_tag.router.models import StepSpec
from media_tag.router.runtime import LocalExecutor
from media_tag.contracts.models import Versions, WorkerResult

from .test_ai_drafts import modeled
from .test_visual_fields import visual


@pytest.fixture
def topic(visual):
    v = visual
    v.add()
    # Equivalent durable request alias to the authentic synthetic Router job.
    # This fixture bypasses dispatch only; accepted results still cross all real gates.
    with v.env.database.transaction(write=True) as connection:
        intent = ensure_intent(connection, v.m.sought.asset.asset_id,
            v.env.owner.actor_id, content_kind='image')
        connection.execute('''INSERT INTO router_requests(owner_id,request_id,fingerprint,job_id)
            SELECT owner_id,?,fingerprint,job_id FROM router_requests
            WHERE job_id=? AND owner_id=? LIMIT 1''', (intent, v.job['job_id'], v.env.owner.actor_id))
    v.analysis = AnalysisService(v.env.router)
    return v


def state(v):
    return v.analysis.status(v.m.sought.asset.asset_id, principal=v.env.owner)


def fields(v):
    return {f['field']: f for f in state(v)['topic_analysis']['fields']}


def test_ten_fields_preserve_four_verified_drafts_without_inventing_missing_six_or_approval(topic):
    v = topic
    before = v.env.catalog.get_asset(v.m.sought.asset.asset_id, principal=v.env.owner)
    assert state(v)['topic_analysis']['local_visual_state'] == 'queued'
    assert v.executor.run_once()['accepted']
    result = state(v)
    document = result['topic_analysis']
    by_field = fields(v)
    assert document['schema_version'] == 'ct-topic-analysis-view-v1'
    assert document['document_schema_version'] == 'ct-topic-analysis-v1'
    assert len(by_field) == 10
    assert {name for name, value in by_field.items() if value['value']['state'] == 'observed'} == {
        '画面主体', '表现形式', '中文检索词', '英文检索词'}
    assert by_field['画面主体']['value']['values'] == [v.options.caption['subject']]
    assert by_field['画面主体']['next_step'] == 'precision'
    assert by_field['疾病/症状']['value']['state'] == 'unknown'
    assert by_field['疾病/症状']['requires_field_review']
    assert by_field['疾病/症状']['next_step'] == 'local'
    assert document['local_visual_state'] == 'verified_local_draft'
    assert not document['analysis_complete'] and not document['precision_exemption_calibrated']
    assert not document['medical_confirmation'] and not document['privacy_confirmation']
    assert not document['effective_values_written']
    assert len(document['producers']) == len(document['evidence']) == 1
    assert by_field['画面主体']['producer_ids'] == [document['producers'][0]['producer_id']]
    assert document['producers'][0]['report_sha256']
    assert state(v)['topic_analysis']['analysis_id'] == document['analysis_id']
    encoded = json.dumps(document)
    assert 'jobs/' not in encoded and str(v.env.workspace) not in encoded and '"ref"' not in encoded
    after = v.env.catalog.get_asset(before.asset_id, principal=v.env.owner)
    assert before.revision == after.revision and before.effective_values == after.effective_values


@pytest.mark.parametrize('artifact', ['report', 'frame'])
def test_each_read_reverifies_artifacts_and_removes_drafts_on_tamper(topic, artifact):
    v = topic
    v.executor.run_once()
    document = state(v)['topic_analysis']
    with v.env.database.transaction() as connection:
        row = connection.execute('''SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id)
            WHERE job_id=? AND active=1 AND json_extract(spec_json,'$.stage')='visual_fields' ''',
            (v.job['job_id'],)).fetchone()
    result = json.loads(row['result_json'])
    ref = result['artifacts'][0]['ref'] if artifact == 'report' else result['evidence'][0]['artifact']['ref']
    (v.env.workspace / ref).write_bytes(b'synthetic corruption')
    updated = state(v)['topic_analysis']
    assert updated['analysis_id'] != document['analysis_id']
    assert updated['local_visual_state'] == 'unavailable'
    assert not updated['producers'] and not updated['evidence']
    assert all(item['value']['state'] != 'observed' for item in updated['fields'])
    assert fields(v)['画面主体']['next_step'] == 'evidence'


def test_new_pending_attempt_and_cancellation_never_fall_back_to_old_success(topic):
    v = topic
    v.executor.run_once()
    assert fields(v)['画面主体']['value']['state'] == 'observed'
    with v.env.database.transaction() as connection:
        row = connection.execute('''SELECT spec_json FROM router_tasks WHERE job_id=?
            AND active=1 AND json_extract(spec_json,'$.stage')='visual_fields' ''', (v.job['job_id'],)).fetchone()
    job = v.env.router.get_job(v.job['job_id'], principal=v.env.owner)
    v.env.router.replan(job['job_id'], (StepSpec.model_validate_json(row['spec_json']),),
        expected_revision=job['revision'], principal=v.env.owner)
    assert fields(v)['画面主体']['value']['state'] == 'unknown'
    v.env.router.cancel(job['job_id'], principal=v.env.owner)
    assert not state(v)['topic_analysis']['producers']
    assert fields(v)['画面主体']['value']['state'] == 'unknown'


def test_outside_form_remains_unmapped_and_does_not_trigger_false_exemption(topic):
    v = topic
    v.options.caption['form'] = ''
    v.options.normalizations = ['form_outside_template_left_unknown']
    v.executor.run_once()
    form = fields(v)['表现形式']
    assert form['value']['state'] == 'no_match' and form['next_step'] == 'vocabulary'
    assert fields(v)['画面主体']['value']['state'] == 'observed'


def test_requester_without_edit_permission_gets_no_content_projection(topic):
    v = topic
    v.executor.run_once()
    other = Principal(actor_id='synthetic-topic-reader')
    asset = v.env.catalog.get_asset(v.m.sought.asset.asset_id, principal=v.env.owner)
    v.env.catalog.change_access(asset.asset_id, principal=v.env.owner, expected_revision=asset.revision,
        subject_id=other.actor_id, permission='read', reason='synthetic read only')
    with v.env.database.transaction(write=True) as connection:
        ensure_intent(connection, asset.asset_id, other.actor_id, content_kind='image')
    result = v.analysis.status(asset.asset_id, principal=other)
    assert result['state'] == 'awaiting_edit_permission' and result['topic_analysis'] is None


def test_clip_missing_vocabulary_does_not_block_verified_visual_field_projection(topic):
    v = topic
    v.executor.run_once()
    job = v.env.router.get_job(v.job['job_id'], principal=v.env.owner)
    clip = StepSpec(key='clip', stage='clip', depends_on=('evidence',),
        versions=Versions(rules_version='synthetic', model_revision=v.m.revision, preprocess_version='synthetic-frames'))
    v.env.router.replan(job['job_id'], (clip,), expected_revision=job['revision'], principal=v.env.owner)
    v.env.router.register_worker(v.m.worker.actor_id, ('clip',), principal=v.env.admin)
    assert LocalExecutor(v.env.router, v.m.worker, v.m.handler).run_once()['accepted']
    assert state(v)['runtime']['state'] == 'needs_attention'
    assert state(v)['topic_analysis']['local_visual_state'] == 'verified_local_draft'
    assert fields(v)['画面主体']['value']['state'] == 'observed'


def test_accepted_result_is_not_projected_until_actual_lease_release(topic):
    v = topic
    lease = v.env.router.claim(principal=v.m.worker)
    task = lease.task
    payload = v.executor.handler(lease, threading.Event())
    result = WorkerResult(task_id=task.task_id, attempt=task.attempt, lease_epoch=task.lease_epoch,
        input_fingerprint=task.input_fingerprint, versions=task.versions, elapsed_seconds=0.1, **payload)
    assert v.env.router.complete(result, principal=v.m.worker)['accepted']
    try:
        assert state(v)['topic_analysis']['local_visual_state'] == 'awaiting_release'
        assert fields(v)['画面主体']['value']['state'] == 'unknown'
    finally:
        v.env.router.release(task.task_id, task.lease_epoch, principal=v.m.worker)
    assert fields(v)['画面主体']['value']['state'] == 'observed'


def test_http_projection_is_authenticated_requester_scoped_and_never_dispatches(topic):
    v = topic
    v.executor.run_once()
    client = TestClient(v.m.sought.app)
    path = '/analysis/assets/'+v.m.sought.asset.asset_id
    with v.env.database.transaction() as connection:
        before = connection.execute('SELECT COUNT(*) FROM router_jobs').fetchone()[0]
    assert client.get(path).status_code == 401
    assert client.get(path, headers={'Authorization': 'Bearer worker'}).status_code == 403
    assert client.get(path, headers={'Authorization': 'Bearer other'}).status_code == 403
    reader = client.get(path, headers={'Authorization': 'Bearer reader'})
    assert reader.status_code == 200 and reader.json()['state'] == 'not_requested'
    response = client.get(path, headers={'Authorization': 'Bearer owner'})
    assert response.status_code == 200
    document = response.json()['topic_analysis']
    assert len(document['fields']) == 10 and document['producers']
    assert 'jobs/' not in response.text and str(v.env.workspace) not in response.text
    with v.env.database.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM router_jobs').fetchone()[0] == before
