"""Local audio adoption through the real analysis/index/review mainline."""
from media_tag.audio_topics.runtime import AudioTopicHandlers
from media_tag.audio_topics.service import request_audio_topics
from media_tag.drafts.service import DraftIndex
from media_tag.precision.service import request_precision
from media_tag.router.runtime import LocalExecutor

from .test_runtime import audio, setup, submission, rows


def run_local(p):
    setup(p)
    asset = audio(p)
    deploy = p.deployment()
    p.analysis.audio_deployment = deploy
    accepted = request_audio_topics(p.analysis, deploy, asset.asset_id, submission(p, asset), principal=p.env.owner)
    LocalExecutor(p.env.router, p.env.worker, AudioTopicHandlers(p.env.router, p.env.worker, deploy)).run_once()
    return asset, deploy, accepted


def index(p, asset):
    with p.env.database.transaction(write=True) as connection:
        row = connection.execute('SELECT * FROM assets WHERE asset_id=?', (asset.asset_id,)).fetchone()
        return DraftIndex(p.env.router).sync(connection, row)


def test_actual_audio_report_reaches_status_index_and_current_review(precise):
    p = precise
    asset, _, accepted = run_local(p)
    view = p.analysis.status(asset.asset_id, principal=p.env.owner)
    assert not view['analysis_complete']
    assert set(view['topic_analysis']['audio_topic_stages'].values()) == {'verified_audio_topics_draft'}
    fields = {item['field']: item for item in view['topic_analysis']['fields']}
    assert fields['中文检索词']['origin'] == 'local'
    assert fields['中文检索词']['value']['state'] == 'observed'
    assert fields['中文检索词']['producer_ids']
    indexed = index(p, asset)
    assert indexed['stages']['audio_topics'] == 'ready'
    labels = [item for item in indexed['labels'] if item.get('field') == '中文检索词']
    assert labels and all(item['origin'] == 'local' for item in labels)
    assert indexed['topic_field_provenance']['中文检索词']['task_id'] == accepted['task_id']
    detail = p.sought.reviews.detail(asset.asset_id, principal=p.env.owner)
    assert len(detail['suggestions']) == 8 and all(item['available'] for item in detail['suggestions'])
    assert all(item['evidence'][0]['kind'] == 'audio_segment' for item in detail['suggestions'])


def test_new_precision_field_scope_masks_only_that_local_audio_field(precise):
    p = precise
    asset, deploy, accepted = run_local(p)
    request_precision(p.analysis, deploy, asset.asset_id,
        submission(p, asset, request='precision-one-field', fields=('中文检索词',)), principal=p.env.owner)
    view = p.analysis.status(asset.asset_id, principal=p.env.owner)
    fields = {item['field']: item for item in view['topic_analysis']['fields']}
    assert fields['中文检索词']['value']['state'] == 'unknown'
    assert fields['中文检索词']['reason'] == 'precision_task_pending'
    assert fields['英文检索词']['value']['state'] == 'observed'
    indexed = index(p, asset)
    assert not any(item.get('field') == '中文检索词' for item in indexed['labels'])
    assert any(item.get('field') == '英文检索词' and item['origin'] == 'local' for item in indexed['labels'])
    detail = p.sought.reviews.detail(asset.asset_id, principal=p.env.owner)
    assert not next(item for item in detail['suggestions'] if item['field'] == '中文检索词')['available']
    assert next(item for item in detail['suggestions'] if item['field'] == '英文检索词')['available']


def test_corrupt_current_audio_report_invalidates_status_cached_index_and_review(precise):
    import json
    p = precise
    asset, _, accepted = run_local(p)
    assert index(p, asset)['labels']
    row, _ = rows(p, asset.asset_id, accepted['task_id'])
    ref = next(item for item in json.loads(row['result_json'])['artifacts'] if item['kind']=='precision_report')
    (p.env.router.workspace/ref['ref']).write_bytes(b'{}')
    view = p.analysis.status(asset.asset_id, principal=p.env.owner)
    fields = {item['field']: item for item in view['topic_analysis']['fields']}
    assert fields['中文检索词']['value']['state'] == 'failed'
    assert not index(p, asset)['labels']
    assert not any(item['available'] for item in p.sought.reviews.detail(asset.asset_id, principal=p.env.owner)['suggestions'])


def test_opt_in_local_audio_dispatch_is_bounded_and_does_not_repeat_failure(precise):
    from media_tag.audio_topics.service import dispatch_pending
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment('wrong_source', auto_dispatch=True, dispatch_limit=1)
    accepted = dispatch_pending(p.analysis, deploy, principal=p.env.owner)
    assert len(accepted) == 1 and accepted[0]['asset_id'] == asset.asset_id
    LocalExecutor(p.env.router, p.env.worker, AudioTopicHandlers(p.env.router, p.env.worker, deploy)).run_once()
    row, _ = rows(p, asset.asset_id, accepted[0]['task_id'])
    assert row['state'] == 'needs_attention'
    assert dispatch_pending(p.analysis, deploy, principal=p.env.owner) == []


def test_local_audio_auto_dispatch_does_not_replace_preexisting_precision_fields(precise):
    from media_tag.audio_topics.service import dispatch_pending
    from media_tag.precision.runtime import PrecisionHandlers
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment(auto_dispatch=True)
    accepted = request_precision(p.analysis, deploy, asset.asset_id,
        submission(p, asset, request='precision-keywords-first', fields=('中文检索词',)), principal=p.env.owner)
    assert dispatch_pending(p.analysis, deploy, principal=p.env.owner) == []
    LocalExecutor(p.env.router, p.env.worker, PrecisionHandlers(p.env.router, p.env.worker, deploy)).run_once()
    local = dispatch_pending(p.analysis, deploy, principal=p.env.owner)
    assert len(local) == 1 and len(local[0]['fields']) == 7
    assert '中文检索词' not in local[0]['fields']
    fields = {item['field']: item for item in p.analysis.status(asset.asset_id, principal=p.env.owner)['topic_analysis']['fields']}
    assert fields['中文检索词']['origin'] == 'precision'
    assert fields['中文检索词']['value']['state'] == 'observed'


def test_explicit_audio_repair_reuses_probe_and_replaces_only_corrupt_attempt(precise):
    import json
    from media_tag.analysis.repair import RepairSubmission, repair
    p = precise
    asset, deploy, accepted = run_local(p)
    row, _ = rows(p, asset.asset_id, accepted['task_id'])
    report = next(item for item in json.loads(row['result_json'])['artifacts'] if item['kind']=='precision_report')
    (p.env.router.workspace/report['ref']).write_bytes(b'{}')
    spec = RepairSubmission(**submission(p, asset, request='repair-audio', fields=('中文检索词',)).model_dump())
    outcome = repair(p.analysis, asset.asset_id, spec, principal=p.env.owner)
    assert outcome['stage_assessments'][row['step_key']] == 'unavailable'
    assert outcome['patched_roots'] == [row['step_key']]
    assert 'probe' in outcome['reused_stage_keys']
    assert not outcome['pending_capability_fields']
    assert repair(p.analysis, asset.asset_id, spec, principal=p.env.owner)['reused_request']
    LocalExecutor(p.env.router, p.env.worker, AudioTopicHandlers(p.env.router, p.env.worker, deploy)).run_once()
    fields = {item['field']:item for item in p.analysis.status(asset.asset_id, principal=p.env.owner)['topic_analysis']['fields']}
    assert fields['中文检索词']['value']['state'] == 'observed'


def test_actual_audio_observation_can_confirm_one_field_and_rerun_preserves_it(precise):
    import json
    from media_tag.analysis.repair import RepairSubmission, repair
    from media_tag.review.models import Observation, FieldDecision
    p = precise
    asset, deploy, accepted = run_local(p)
    reviews = p.sought.reviews
    observation = reviews.observe(asset.asset_id, Observation(request_id='synthetic-listen',
        expected_revision=asset.revision, source_sha256=asset.sha256, kind='audio_segment', viewed=True,
        start_seconds=0, end_seconds=.5, note='Synthetic auditory protocol observation, not business acceptance'), principal=p.env.owner)
    candidate = next(item for item in reviews.detail(asset.asset_id, principal=p.env.owner)['suggestions'] if item['field']=='中文检索词')
    reviews.decide(asset.asset_id, FieldDecision(request_id='synthetic-confirm-audio-keywords',
        expected_revision=asset.revision, source_sha256=asset.sha256, field='中文检索词', decision='accept',
        run_id=candidate['run_id'], suggestion_id=candidate['suggestion_id'],
        observation_ids=(observation['observation_id'],), note='Synthetic one-field confirmation'), principal=p.env.owner)
    confirmed = p.env.catalog.get_asset(asset.asset_id, principal=p.env.owner).effective_values
    assert any(item.field=='中文检索词' for item in confirmed)
    assert not any(item.get('field')=='中文检索词' for item in index(p, asset)['labels'])
    row, _ = rows(p, asset.asset_id, accepted['task_id'])
    report = next(item for item in json.loads(row['result_json'])['artifacts'] if item['kind']=='precision_report')
    (p.env.router.workspace/report['ref']).write_bytes(b'{}')
    repair(p.analysis, asset.asset_id, RepairSubmission(**submission(p, asset,
        request='repair-with-human-value', fields=('中文检索词',)).model_dump()), principal=p.env.owner)
    LocalExecutor(p.env.router, p.env.worker, AudioTopicHandlers(p.env.router, p.env.worker, deploy)).run_once()
    assert p.env.catalog.get_asset(asset.asset_id, principal=p.env.owner).effective_values == confirmed
