from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient

from media_tag.api.app import create_app
from media_tag.api.config import LocalConfig, executor_from_config
from media_tag.audio_topics.runtime import AUDIO_FIELDS

from .test_runtime import audio, setup, submission


def test_audio_http_preserves_actor_and_strict_contract_with_separate_configuration(precise):
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment()
    app = create_app(p.env.router, {sha256(name.encode()).hexdigest(): actor for name, actor in
        [('owner', p.env.owner), ('worker', p.env.worker), ('other', p.env.other)]},
        analysis_options=p.analysis.options, audio_topics_options=deploy)
    client = TestClient(app)
    path = '/analysis/assets/'+asset.asset_id+'/audio-topics'
    payload = submission(p, asset).model_dump(mode='json')
    assert client.post(path, json=payload).status_code == 401
    for actor in ('worker', 'other'):
        assert client.post(path, json=payload, headers={'Authorization':'Bearer '+actor}).status_code == 403
    headers = {'Authorization':'Bearer owner'}
    assert client.post(path, json=payload | {'principal': {'actor_id':'spoof'}}, headers=headers).status_code == 422
    response = client.post(path, json=payload, headers=headers)
    assert response.status_code == 200 and set(response.json()['fields']) == set(AUDIO_FIELDS)
    assert client.post(path, json=payload, headers=headers).json()['reused_request']
    precision = client.post(path.removesuffix('audio-topics')+'precision',
        json=submission(p, asset, request='unconfigured-precision').model_dump(mode='json'), headers=headers)
    assert precision.status_code == 409


def test_configured_worker_auto_dispatches_local_audio_without_enabling_precision(precise, tmp_path):
    p = precise
    setup(p)
    asset = audio(p)
    deploy = p.deployment(auto_dispatch=True)
    config = LocalConfig(workspace=str(p.env.workspace), database=str(p.env.database.path),
        object_store=str(p.env.catalog.object_store.root), template_dir=str((Path.cwd()/'asset-index-template').resolve()),
        template_fingerprint=p.env.contract.fingerprint, credentials={sha256(b'owner').hexdigest():p.env.owner,
            sha256(b'worker').hexdigest():p.env.worker}, workers=(p.env.worker.actor_id,), tool_dir=str(p.tools.ffmpeg.parent),
        analysis=p.analysis.options, audio_topics=deploy, review=p.sought.reviews.config)
    assert not config.precision.enabled
    path = tmp_path/'synthetic-audio.local.json'
    path.write_text(config.model_dump_json(), encoding='utf8')
    executor = executor_from_config(path, p.env.worker.actor_id)
    assert executor.run_once()['accepted']
    assert 'audio_topics' in executor.router.stages and 'precision' not in executor.router.stages
    view = p.analysis.status(asset.asset_id, principal=p.env.owner)
    assert set(view['topic_analysis']['audio_topic_stages'].values()) == {'verified_audio_topics_draft'}
    assert not view['analysis_complete']
    assert executor.run_once() is None
