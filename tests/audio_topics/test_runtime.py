"""Real WAV and isolated synthetic bridge; no audio model inference."""
from io import BytesIO
import json
import math
import struct
import wave

import pytest

from media_tag.audio_topics.projection import verified
from media_tag.audio_topics.runtime import AUDIO_FIELDS, AudioTopicHandlers, stage
from media_tag.audio_topics.service import request_audio_topics
from media_tag.catalog import AccessDenied, RevisionConflict
from media_tag.precision.contracts import PrecisionDeployment
from media_tag.precision.projection import verified as precision_verified
from media_tag.precision.service import PrecisionSubmission, request_precision
from media_tag.router.runtime import LocalExecutor

pytestmark = pytest.mark.usefixtures('tools')


def audio(p):
    stream = BytesIO()
    with wave.open(stream, 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(8000)
        target.writeframes(b''.join(struct.pack('<h', int(8000*math.sin(2*math.pi*440*n/8000)))
            for n in range(4000)))
    return p.register(stream.getvalue(), 'actual-tone.wav', 'audio')


def setup(p):
    e = p.env
    e.router.stages['audio_topics'] = stage()
    e.router.register_worker(e.worker.actor_id, ('probe', 'evidence', 'precision', 'audio_topics'), principal=e.admin)


def submission(p, asset, *, request='local-audio-request', fields=None):
    current = p.env.catalog.get_asset(asset.asset_id, principal=p.env.owner)
    state = p.analysis.status(asset.asset_id, principal=p.env.owner)
    return PrecisionSubmission(request_id=request, expected_asset_revision=current.revision,
        source_sha256=current.sha256, expected_job_revision=state['runtime']['revision'],
        **({'fields': fields} if fields is not None else {}))


def rows(p, asset_id, task_id):
    with p.env.database.transaction() as connection:
        asset = dict(connection.execute('SELECT * FROM assets WHERE asset_id=?', (asset_id,)).fetchone())
        row = dict(connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at
            FROM router_tasks t LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE t.task_id=?''', (task_id,)).fetchone())
    return row, asset


def test_real_wav_local_layer_eight_fields_and_no_effective_value_writes(precise):
    p = precise
    setup(p)
    asset = audio(p)
    before = p.env.catalog.get_asset(asset.asset_id, principal=p.env.owner)
    deploy = p.deployment()
    accepted = request_audio_topics(p.analysis, deploy, asset.asset_id, submission(p, asset), principal=p.env.owner)
    assert set(accepted['fields']) == set(AUDIO_FIELDS) and len(accepted['fields']) == 8
    assert not accepted['model_capability_acceptance'] and not accepted['effective_values_written']
    handler = AudioTopicHandlers(p.env.router, p.env.worker, deploy)
    errors = []
    def capture(lease, stopped):
        try:
            return handler(lease, stopped)
        except Exception as error:
            errors.append(repr(error))
            raise
    result = LocalExecutor(p.env.router, p.env.worker, capture).run_once()
    assert result['accepted']
    row, db_asset = rows(p, asset.asset_id, accepted['task_id'])
    assert json.loads(row['result_json'])['status'] == 'succeeded', errors or row['result_json']
    task, result, report, ref = verified(p.env.router, row, db_asset)
    assert row['state'] == 'succeeded' and row['released_at'] is not None
    assert task.stage == 'audio_topics' and report.analysis_layer == 'local_audio'
    assert report.request_id.startswith('local-audio-')
    fields = {item.field: item for item in report.document.fields}
    assert len(fields) == 10
    for field in ('画面主体', '表现形式'):
        assert fields[field].value.state == 'not_applicable' and fields[field].reason == 'pure_audio'
    for field in AUDIO_FIELDS:
        item = fields[field]
        assert item.origin == 'local' and item.next_step != 'none'
        assert item.reason in ('local_audio_uncalibrated_draft', 'local_audio_applicability_requires_review')
        assert item.evidence_ids == ('precision-content',)
    assert fields['情绪/语气'].next_step == 'precision'
    assert fields['情绪/语气'].value.state == 'unknown'
    assert next(item for item in report.reply.fields if item.field=='情绪/语气').value.state == 'not_applicable'
    assert fields['疾病/症状'].next_step == 'human'
    evidence, = report.document.evidence
    assert evidence.kind == 'audio_segment' and evidence.start_seconds == 0 and evidence.end_seconds == .5
    assert evidence.artifact.sha256 == asset.sha256
    after = p.env.catalog.get_asset(asset.asset_id, principal=p.env.owner)
    assert after.revision == before.revision
    assert after.effective_values == before.effective_values
    with pytest.raises(ValueError, match='binding changed'):
        precision_verified(p.env.router, row, db_asset, 'audio')


def test_local_and_precision_receipts_and_tasks_are_distinct_and_idempotent(precise):
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment()
    spec = submission(p, asset, request='same-external-request')
    first = request_audio_topics(p.analysis, deploy, asset.asset_id, spec, principal=p.env.owner)
    assert request_audio_topics(p.analysis, deploy, asset.asset_id, spec, principal=p.env.owner)['reused_request']
    changed = spec.model_copy(update={'fields': ('情绪/语气',)})
    with pytest.raises(RevisionConflict, match='reused with different input'):
        request_audio_topics(p.analysis, deploy, asset.asset_id, changed, principal=p.env.owner)
    second = request_precision(p.analysis, deploy, asset.asset_id,
        submission(p, asset, request=spec.request_id), principal=p.env.owner)
    assert first['task_id'] != second['task_id']
    with p.env.database.transaction() as connection:
        events = connection.execute("SELECT action FROM router_events WHERE action IN ('audio_topics_request','precision_request')").fetchall()
    assert {item[0] for item in events} == {'audio_topics_request', 'precision_request'}


def test_unselected_audio_provider_and_visual_source_cannot_start_audio_stage(precise):
    p = precise
    setup(p)
    asset = audio(p)
    spec = submission(p, asset)
    with pytest.raises(RevisionConflict, match='provider unavailable'):
        request_audio_topics(p.analysis, PrecisionDeployment(), asset.asset_id, spec, principal=p.env.owner)
    assert p.analysis.status(asset.asset_id, principal=p.env.owner)['runtime']['revision'] == spec.expected_job_revision
    visual = p.env.catalog.get_asset(p.sought.asset.asset_id, principal=p.env.owner)
    with pytest.raises(RevisionConflict, match='actual audio'):
        request_audio_topics(p.analysis, p.deployment(), visual.asset_id, submission(p, visual), principal=p.env.owner)


def test_audio_request_rechecks_actor_and_source_job_revisions(precise):
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment()
    spec = submission(p, asset)
    with pytest.raises(AccessDenied):
        request_audio_topics(p.analysis, deploy, asset.asset_id, spec, principal=p.env.other)
    with pytest.raises(RevisionConflict):
        request_audio_topics(p.analysis, deploy, asset.asset_id,
            spec.model_copy(update={'source_sha256': '0'*64}), principal=p.env.owner)
    with pytest.raises(RevisionConflict):
        request_audio_topics(p.analysis, deploy, asset.asset_id,
            spec.model_copy(update={'expected_job_revision': spec.expected_job_revision+1}), principal=p.env.owner)
    assert p.analysis.status(asset.asset_id, principal=p.env.owner)['runtime']['revision'] == spec.expected_job_revision


def test_audio_task_enforces_input_budget_without_adopting_suggestions(precise):
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment(max_input_bytes=1024)
    accepted = request_audio_topics(p.analysis, deploy, asset.asset_id, submission(p, asset), principal=p.env.owner)
    LocalExecutor(p.env.router, p.env.worker, AudioTopicHandlers(p.env.router, p.env.worker, deploy)).run_once()
    row, _ = rows(p, asset.asset_id, accepted['task_id'])
    result = json.loads(row['result_json'])
    assert result['status'] == 'failed' and result['errors'][0]['code'] == 'precision_input_budget'
    assert not result['suggestions']


@pytest.mark.parametrize('mode', ['unknown', 'outside'])
def test_uncertain_audio_and_unpublished_candidates_remain_unresolved(precise, mode):
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment(mode)
    accepted = request_audio_topics(p.analysis, deploy, asset.asset_id, submission(p, asset), principal=p.env.owner)
    LocalExecutor(p.env.router, p.env.worker, AudioTopicHandlers(p.env.router, p.env.worker, deploy)).run_once()
    row, db_asset = rows(p, asset.asset_id, accepted['task_id'])
    _, _, report, _ = verified(p.env.router, row, db_asset)
    fields = {item.field: item for item in report.document.fields}
    if mode == 'unknown':
        assert all(fields[name].value.state == 'unknown' for name in AUDIO_FIELDS)
        assert fields['情绪/语气'].next_step == 'precision'
    else:
        assert fields['疾病/症状'].value.state == 'no_match'
        assert fields['疾病/症状'].candidates == ('合成未发布候选',)
        assert fields['疾病/症状'].next_step == 'vocabulary'


@pytest.mark.parametrize('mode', ['forged_approval', 'wrong_source'])
def test_local_audio_rejects_forged_approval_and_wrong_source_reply(precise, mode):
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment(mode)
    accepted = request_audio_topics(p.analysis, deploy, asset.asset_id, submission(p, asset), principal=p.env.owner)
    LocalExecutor(p.env.router, p.env.worker, AudioTopicHandlers(p.env.router, p.env.worker, deploy)).run_once()
    row, _ = rows(p, asset.asset_id, accepted['task_id'])
    result = json.loads(row['result_json'])
    assert result['status'] == 'failed' and not result['suggestions']
