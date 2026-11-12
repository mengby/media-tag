"""Regression for extending an already tagged library without replaying existing fields."""
import csv
from io import StringIO
import json
import os

import pytest

from local_topics.test_router import topic_router
from local_topics.test_topics import answer
from precision.conftest import precise
from upload.conftest import tools
from media_tag.analysis.fields import DEFAULT_FIELDS_FILE, TagFields
from media_tag.local_topics.contracts import Generation
from media_tag.local_topics.prompt import PROMPT_VERSION
from media_tag.review.service import ReviewService
from media_tag.router.runtime import LocalExecutor
from .test_topic_batch import HEADERS, run_static


def test_add_field_after_tagging_then_search_and_export(topic_router, tools, tmp_path, monkeypatch):
    t = topic_router
    client, app, body = run_static(t, tools)
    asset_id = body['assets'][0]['asset_id']
    registry = json.loads(DEFAULT_FIELDS_FILE.read_text(encoding='utf8'))
    config_file = tmp_path/'fields.json'
    asset_values = {'口语化表达': '像个小红圈', '画面里的形状': '圆圆的一圈'}
    requested = []

    def configure():
        config_file.write_text(json.dumps(registry, ensure_ascii=False), encoding='utf8')
        # Set the deployment path once. The second edit must be picked up by the same running services.
        if t.sought.reviews.policy.config.tag_fields_file is None:
            review = ReviewService(t.env.router, t.sought.reviews.config.model_copy(update={'tag_fields_file': str(config_file)}))
            t.sought.reviews.policy = review.policy

    def generate(request, output, task, stopped, *, deadline):
        name, = task.target_fields
        prompt, = request['field_prompts']
        assert t.sought.reviews.policy.tag_fields.guidance[name] in prompt
        requested.append(name)
        return Generation(backend='local_qwen', model_revision=task.versions.model_revision,
            prompt_version=PROMPT_VERSION, image_sha256=request['image_sha256'], answers=(answer(name, (asset_values[name],)),))

    monkeypatch.setattr(t.handler, 'invoke', generate)
    executor = LocalExecutor(t.env.router, t.env.worker, t.handler)

    def tasks():
        with t.env.database.transaction() as c:
            return dict(c.execute('SELECT task_id,lease_epoch FROM router_tasks'))

    def submit():
        result = client.post('/search/auto-tag/backfill', json=body, headers=HEADERS)
        assert result.status_code == 200, result.text
        return result.json()

    def snapshot():
        result = client.post('/search/auto-tag/export', json=body, headers=HEADERS)
        assert result.status_code == 200, result.text
        manifest = result.json()['manifest']
        raw = client.get(result.json()['assets_url'], headers=HEADERS)
        assert raw.status_code == 200
        row, = csv.DictReader(StringIO(raw.content.decode('utf-8-sig')))
        assert len(row) == manifest['asset_columns']
        return row

    for name in asset_values:
        if name != '口语化表达':
            registry['fields'].append({'name': name, 'prompt': '用一个短语描述实际可见的形状。'})
        configure()
        assert snapshot()[name] == ''
        before = tasks()
        assert submit()['items'][0]['fields'] == [name]
        assert submit()['queued'] == 0
        assert executor.run_once()['accepted']
        assert executor.run_once() is None
        assert submit()['queued'] == 0
        after = tasks()
        assert len(after) == len(before)+1 and all(after[key] == epoch for key, epoch in before.items())
        row = snapshot()
        assert len(row) == 35+len(requested) and row['画面主体'] == '合成圆形'
        for field in requested:
            assert row[field] == asset_values[field]
            response = client.post('/v1/assets/search', headers=HEADERS, json={
                'query': asset_values[field], 'include_drafts': True, 'literal_only': True, 'use_model': False})
            assert response.status_code == 200
            assert asset_id in [item['asset_id'] for item in response.json()['items']]
    assert requested == list(asset_values)
    legacy = client.post('/review/exports', headers=HEADERS, json={
        **body, 'request_id': 'legacy-35', 'include_configured_fields': False, 'include_ai_drafts': True})
    assert legacy.status_code == 200 and legacy.json()['manifest']['asset_columns'] == 35


def test_legacy_library_job_backfills_without_an_analysis_intent(topic_router, tools):
    t = topic_router
    client, _, body = run_static(t, tools)
    asset_id = body['assets'][0]['asset_id']
    with t.env.database.transaction(write=True) as connection:
        connection.execute('DELETE FROM analysis_intents WHERE asset_id=?', (asset_id,))
        before = dict(connection.execute('SELECT task_id,lease_epoch FROM router_tasks'))
    review = ReviewService(t.env.router, t.sought.reviews.config.model_copy(update={'tag_fields_file': str(DEFAULT_FIELDS_FILE)}))
    t.sought.reviews.policy = review.policy
    result = client.post('/search/auto-tag/backfill', json=body, headers=HEADERS).json()
    assert result['queued'] == 1 and result['items'][0]['fields'] == ['口语化表达']
    with t.env.database.transaction() as connection:
        after = dict(connection.execute('SELECT task_id,lease_epoch FROM router_tasks'))
        assert len(after) == len(before) + 1 and before.items() <= after.items()
        assert not connection.execute('SELECT 1 FROM analysis_intents WHERE asset_id=?', (asset_id,)).fetchone()


@pytest.mark.skipif(not os.environ.get('MEDIA_TAG_TEST_QWEN_ROOT'), reason='real local weights explicitly required')
def test_real_local_worker_generates_only_configured_field(topic_router, tools):
    from media_tag.local_topics.contracts import LocalTopicDeployment
    from media_tag.local_topics.runtime import LocalTopicHandlers
    t = topic_router
    client, app, body = run_static(t, tools)
    review = ReviewService(t.env.router, t.sought.reviews.config.model_copy(update={'tag_fields_file': str(DEFAULT_FIELDS_FILE)}))
    t.sought.reviews.policy = review.policy
    result = client.post('/search/auto-tag/backfill', json=body, headers=HEADERS).json()
    assert result['queued'] == 1
    handler = LocalTopicHandlers(t.env.router, t.env.worker,
                                LocalTopicDeployment(enabled=True, model_root=os.environ['MEDIA_TAG_TEST_QWEN_ROOT']))
    executor = LocalExecutor(t.env.router, t.env.worker, handler)
    assert executor.run_once()['accepted']
    assert executor.run_once() is None
    view = app.state.analysis.status(t.sought.asset.asset_id, principal=t.env.owner)['topic_analysis']
    field = next(f for f in view['fields'] if f['field'] == '口语化表达')
    assert field['origin'] == 'local' and field['value']['state'] in ('observed', 'unknown'), field
    print(json.dumps(field['value'], ensure_ascii=True))


def test_precision_backfills_only_new_field_and_sends_its_guidance(precise, monkeypatch):
    from precision.test_chat import KEY, endpoint, provider
    from media_tag.precision.contracts import PrecisionDeployment
    from media_tag.precision.runtime import PrecisionHandlers
    from .test_topic_batch import client_for, scope

    p = precise
    _, old_task, old_executor = p.submit(p.deployment())
    assert old_executor.run_once()['accepted']
    p.sought.reviews.policy = ReviewService(p.env.router, p.sought.reviews.config.model_copy(
        update={'tag_fields_file': str(DEFAULT_FIELDS_FILE)})).policy
    body = scope(p.env, p.sought.asset.asset_id)
    monkeypatch.setenv('TEST_FIELD_PROVIDER_KEY', KEY)
    with endpoint() as (url, calls):
        remote = provider(url, api_key_env='TEST_FIELD_PROVIDER_KEY')
        deploy = PrecisionDeployment(enabled=True, providers=(remote,), visual_provider=remote.provider_id)
        client, app = client_for(p.sought, precision=deploy)
        from media_tag.precision.service import dispatch_pending
        assert dispatch_pending(app.state.analysis, deploy.model_copy(update={'auto_dispatch': True}), principal=p.env.owner) == []
        planned = client.post('/search/auto-tag/backfill', json=body, headers=HEADERS).json()
        assert planned['items'][0]['fields'] == ['口语化表达'] and planned['queued'] == 1
        p.env.router.register_worker(p.env.worker.actor_id, ('precision',), principal=p.env.admin)
        executor = LocalExecutor(p.env.router, p.env.worker, PrecisionHandlers(p.env.router, p.env.worker, deploy))
        assert executor.run_once()['accepted'] and executor.run_once() is None
        assert client.post('/search/auto-tag/backfill', json=body, headers=HEADERS).json()['queued'] == 0
    assert len(calls) == 1
    sent = json.loads(calls[0][2]['messages'][1]['content'][0]['text'])
    assert sent['fields'] == ['口语化表达'] and sent['field_guidance'] == p.sought.reviews.policy.tag_fields.guidance
    doc = app.state.analysis.status(p.sought.asset.asset_id, principal=p.env.owner)['topic_analysis']
    fields = {field['field']:field for field in doc['fields']}
    assert fields['口语化表达']['origin'] == 'precision' and fields['口语化表达']['value']['state'] == 'unknown'
    assert fields['英文检索词']['producer_ids'] == [old_task['task_id']+'.1']


def test_configured_fields_reject_duplicate_or_original_column(tmp_path):
    path = tmp_path/'fields.json'
    for names in (['口语化表达', '口语化表达'], ['许可状态'], ['疾病/症状'], ['=formula']):
        path.write_text(json.dumps({'fields':[{'name':name,'prompt':'根据实际画面填写。'} for name in names]}), encoding='utf8')
        with pytest.raises(ValueError):
            TagFields.load(str(path), ('许可状态',))
