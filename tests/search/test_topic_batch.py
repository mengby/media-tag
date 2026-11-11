"""Explicit batch/real protocol wiring; no model weights or production data."""
from hashlib import sha256
import json

import pytest
from fastapi.testclient import TestClient

from local_topics.test_router import topic_router
from precision.conftest import precise
from upload.conftest import tools
from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.intake import ensure_intent
from media_tag.analysis.service import AnalysisService
from media_tag.api.app import create_app
from media_tag.router.runtime import CPUHandlers, LocalExecutor, cpu_stages
from media_tag.storage.filesystem import FilesystemBackend


HEADERS={'Authorization':'Bearer owner'}


def scope(env,asset_id):
    asset=env.catalog.get_asset(asset_id,principal=env.owner)
    return {'assets':[{'asset_id':asset.asset_id,'source_sha256':asset.sha256,'expected_revision':asset.revision}]}


def client_for(sought,*,enabled=True,precision=None,audio=None):
    e=sought.env
    app=create_app(e.router,{sha256(name.encode()).hexdigest():getattr(e,name) for name in ('owner','other','reader')},
        web=True,reviews=sought.reviews,terminology=e.service,resolver=sought.resolver,search=sought.search,
        analysis_options=AnalysisDeployment(enabled=enabled),precision_options=precision,audio_topics_options=audio)
    return TestClient(app),app


def run_static(t,tools):
    e=t.env
    e.router.stages.update(cpu_stages(tools))
    e.router.register_worker(e.worker.actor_id,('probe','evidence','local_topics'),principal=e.admin)
    with e.database.transaction(write=True) as c:
        ensure_intent(c,t.sought.asset.asset_id,e.owner.actor_id,content_kind='image')
    client,app=client_for(t.sought)
    body=scope(e,t.sought.asset.asset_id)
    accepted=client.post('/search/auto-tag',json=body,headers=HEADERS)
    assert accepted.status_code==200 and accepted.json()['added']==1
    cpu=CPUHandlers(e.router,e.worker,tools)
    executor=LocalExecutor(e.router,e.worker,lambda lease,stopped:
        t.handler(lease,stopped) if lease.task.stage=='local_topics' else cpu(lease,stopped))
    for _ in range(12):
        if executor.run_once() is None:break
    return client,app,body


def test_local_topics_only_button_batch_status_and_export_are_actual_and_idempotent(topic_router,tools):
    t=topic_router;e=t.env
    client,app,body=run_static(t,tools)
    assert 'visual_fields' not in e.router.stages
    assert client.get('/ui/session',headers=HEADERS).json()['features']['semantic_fields']
    status=client.post('/search/auto-tag/status',json=body,headers=HEADERS).json()
    assert status['state']=='ready' and status['execution_complete']
    assert status['completed']==1 and not status['semantic_complete']
    assert status['pending_fields']>0 and status['review_required']==1
    assert not status['medical_confirmation'] and 'jobs/' not in json.dumps(status)
    before=app.state.analysis.status(t.sought.asset.asset_id,principal=e.owner)['runtime']['tasks']
    again=client.post('/search/auto-tag',json=body,headers=HEADERS).json()
    assert again['reused']==1 and again['added']==0
    after=app.state.analysis.status(t.sought.asset.asset_id,principal=e.owner)['runtime']['tasks']
    assert before==after
    exported=client.post('/search/auto-tag/export',json=body,headers=HEADERS)
    assert exported.status_code==200 and exported.json()['assets_url'].endswith('/assets.csv')
    with e.database.transaction() as c:
        assert t.sought.search.library._analysis(c,t.sought.asset.asset_id)=='waiting_review'
    asset=e.catalog.get_asset(t.sought.asset.asset_id,principal=e.owner)
    assert any(v.field=='画面主体' and v.value.values==('合成圆形',) for v in asset.effective_values)


@pytest.mark.parametrize('enabled',[False,True])
def test_missing_models_are_explicit_pending_and_execution_stops_without_semantic_success(sought,tools,enabled):
    e=sought.env;e.router.stages.update(cpu_stages(tools))
    e.router.register_worker(e.worker.actor_id,('probe','evidence'),principal=e.admin)
    client,app=client_for(sought,enabled=enabled)
    body=scope(e,sought.asset.asset_id)
    with e.database.transaction(write=True) as c:
        ensure_intent(c,sought.asset.asset_id,e.owner.actor_id,content_kind='image')
    reply=client.post('/search/auto-tag',json=body,headers=HEADERS)
    assert reply.status_code==200 and not reply.json()['errors']
    if enabled:
        assert client.get('/ui/session',headers=HEADERS).json()['features']['semantic_fields']
        executor=LocalExecutor(e.router,e.worker,CPUHandlers(e.router,e.worker,tools))
        for _ in range(8):
            if executor.run_once() is None:break
    status=client.post('/search/auto-tag/status',json=body,headers=HEADERS).json()
    assert status['execution_complete'] and status['waiting']==1 and status['pending_capability']==1
    assert not status['semantic_complete'] and status['pending_fields']==10
    assert not status['medical_confirmation']
    if not enabled:
        assert not app.state.analysis.status(sought.asset.asset_id,principal=e.owner)['runtime_dispatched']


def test_selected_batch_does_not_scan_other_intents_or_borrow_owner_identity(sought,tools):
    e=sought.env;e.router.stages.update(cpu_stages(tools))
    client,app=client_for(sought)
    other=e.catalog.register_file(
        FilesystemBackend(sought.original.parent),
        sought.original.name,source_backend_id='synthetic-unselected',principal=e.owner,request_id='unselected',name='Unselected')
    body=scope(e,sought.asset.asset_id)
    assert client.post('/search/auto-tag',json=body,headers=HEADERS).status_code==200
    assert not app.state.analysis.status(other.asset_id,principal=e.owner)['runtime_dispatched']
    assert client.post('/search/auto-tag',json=body,headers={'Authorization':'Bearer other'}).status_code==403
    assert client.post('/search/auto-tag',json=body,headers={'Authorization':'Bearer reader'}).status_code==403
    bad={'assets':[dict(body['assets'][0],source_sha256='0'*64)]}
    assert client.post('/search/auto-tag',json=bad,headers=HEADERS).status_code==409


def test_batch_repairs_only_current_damaged_static_attempt_and_not_model_version_change(topic_router,tools):
    t=topic_router;e=t.env
    client,app,body=run_static(t,tools)
    state=app.state.analysis.status(t.sought.asset.asset_id,principal=e.owner)
    before={task['stage']:task['task_id'] for task in state['runtime']['tasks']}
    with e.database.transaction() as c:
        result=c.execute('''SELECT a.result_json FROM router_tasks t JOIN router_attempts a
            ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch WHERE t.task_id=?''',(before['local_topics'],)).fetchone()[0]
    report=next(a for a in json.loads(result)['artifacts'] if a['kind']=='local_topic_report')
    (e.workspace/report['ref']).write_text('{}')
    assert client.post('/search/auto-tag',json=body,headers=HEADERS).json()['added']==1
    after={task['stage']:task['task_id'] for task in app.state.analysis.status(t.sought.asset.asset_id,principal=e.owner)['runtime']['tasks']}
    assert after['probe']==before['probe'] and after['local_topics']!=before['local_topics']
    from dataclasses import replace
    e.router.stages['local_topics']=replace(e.router.stages['local_topics'],required_model_revision='f'*64)
    frozen=app.state.analysis.status(t.sought.asset.asset_id,principal=e.owner)['runtime']['tasks']
    reply=client.post('/search/auto-tag',json=body,headers=HEADERS).json()
    assert reply['added']==0 and reply['items'][0]['state']=='awaiting_explicit_version_repair'
    assert app.state.analysis.status(t.sought.asset.asset_id,principal=e.owner)['runtime']['tasks']==frozen


def test_unknown_kind_probe_corruption_is_repaired_without_inventing_content_kind(sought,tools):
    e=sought.env
    with e.database.transaction(write=True) as c:
        c.execute('UPDATE router_jobs SET cancelled=1')
    e.router.stages.update(cpu_stages(tools))
    e.router.register_worker(e.worker.actor_id,('probe','evidence'),principal=e.admin)
    client,app=client_for(sought)
    body=scope(e,sought.asset.asset_id)
    assert client.post('/search/auto-tag',json=body,headers=HEADERS).json()['added']==1
    executor=LocalExecutor(e.router,e.worker,CPUHandlers(e.router,e.worker,tools))
    for _ in range(12):
        state=app.state.analysis.status(sought.asset.asset_id,principal=e.owner)
        if state['runtime']['tasks'][0]['state']=='succeeded':break
        assert executor.run_once()['accepted']
    state=app.state.analysis.status(sought.asset.asset_id,principal=e.owner)
    assert state['content_kind'] is None
    task=state['runtime']['tasks'][0]
    with e.database.transaction() as c:
        result=json.loads(c.execute('SELECT result_json FROM router_attempts WHERE task_id=?',(task['task_id'],)).fetchone()[0])
    report=next(a for a in result['artifacts'] if a['kind']=='technical_report')
    (e.workspace/report['ref']).write_text('{}')
    repaired=client.post('/search/auto-tag',json=body,headers=HEADERS).json()
    assert repaired['added']==1 and repaired['errors']==[]
    state=app.state.analysis.status(sought.asset.asset_id,principal=e.owner)
    assert state['content_kind'] is None and len(state['runtime']['tasks'])==1
    assert state['runtime']['tasks'][0]['task_id']!=task['task_id']
    for _ in range(12):
        state=app.state.analysis.status(sought.asset.asset_id,principal=e.owner)
        if state['runtime']['tasks'][0]['state']=='succeeded':break
        assert executor.run_once()['accepted']
    assert client.post('/search/auto-tag',json=body,headers=HEADERS).json()['errors']==[]
    assert app.state.analysis.status(sought.asset.asset_id,principal=e.owner)['content_kind']=='image'


def test_audio_topics_and_precision_current_scope_drive_batch_and_library_progress(precise):
    from audio_topics.test_projection import run_local
    p=precise;e=p.env
    asset,deploy,_=run_local(p)
    client,app=client_for(p.sought,audio=deploy,precision=deploy)
    precision_stage=e.router.stages.pop('precision')
    assert client.get('/ui/session',headers=HEADERS).json()['features']['semantic_fields']
    e.router.stages['precision']=precision_stage
    body=scope(e,asset.asset_id)
    status=client.post('/search/auto-tag/status',json=body,headers=HEADERS).json()
    assert status['execution_complete'] and status['completed']==1 and not status['semantic_complete']
    with e.database.transaction() as c:
        assert p.sought.search.library._analysis(c,asset.asset_id)=='waiting_review'
    _,_,executor=p.submit(deploy,asset_id=asset.asset_id,fields=('英文检索词',),request='newer-precision-scope')
    status=client.post('/search/auto-tag/status',json=body,headers=HEADERS).json()
    assert not status['execution_complete'] and status['running']==1
    assert executor.run_once()['accepted']
    status=client.post('/search/auto-tag/status',json=body,headers=HEADERS).json()
    assert status['execution_complete'] and status['completed']==1
    assert client.get('/ui/session',headers=HEADERS).json()['features']['semantic_fields']


def test_failed_precision_is_terminal_and_never_claims_complete(precise):
    p=precise
    deploy=p.deployment('wrong_source')
    _,_,executor=p.submit(deploy)
    assert executor.run_once()['accepted']
    client,app=client_for(p.sought,precision=deploy)
    status=client.post('/search/auto-tag/status',json=scope(p.env,p.sought.asset.asset_id),headers=HEADERS).json()
    assert status['state']=='partial' and status['failed']==1 and status['execution_complete']
    assert not status['semantic_complete'] and not status['medical_confirmation']
    with p.env.database.transaction() as c:
        assert p.sought.search.library._analysis(c,p.sought.asset.asset_id)=='failed'


@pytest.mark.parametrize('auto_dispatch',[False,True])
def test_selected_wav_batch_uses_configured_worker_and_respects_audio_auto_dispatch(precise,tmp_path,auto_dispatch):
    from io import BytesIO
    from pathlib import Path
    import wave
    from audio_topics.test_runtime import setup
    from media_tag.api.config import LocalConfig,executor_from_config

    p=precise;e=p.env;setup(p)
    # Fresh registered WAV has neither a probe result nor an audio semantic task.
    buffer=BytesIO()
    with wave.open(buffer,'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(8000)
        stream.writeframes(b'\xe8\x03\x18\xfc'*2000)
    source=p.sought.original.parent/'batch-tone.wav'
    source.write_bytes(buffer.getvalue())
    asset=e.catalog.register_file(FilesystemBackend(source.parent),source.name,source_backend_id='synthetic-batch-audio',
        principal=e.owner,request_id='batch-tone-source',name='Synthetic selected WAV')
    deploy=p.deployment(auto_dispatch=auto_dispatch)
    client,app=client_for(p.sought,audio=deploy)
    body=scope(e,asset.asset_id)
    response=client.post('/search/auto-tag',json=body,headers=HEADERS)
    assert response.status_code==200 and response.json()['added']==1
    before=app.state.analysis.status(asset.asset_id,principal=e.owner)
    assert before['content_kind'] is None
    assert [task['stage'] for task in before['runtime']['tasks']]==['probe']
    job_id=before['runtime']['job_id']

    config=LocalConfig(workspace=str(e.workspace),database=str(e.database.path),
        object_store=str(e.catalog.object_store.root),template_dir=str((Path.cwd()/'asset-index-template').resolve()),
        template_fingerprint=e.contract.fingerprint,credentials={sha256(b'owner').hexdigest():e.owner,
            sha256(b'worker').hexdigest():e.worker},workers=(e.worker.actor_id,),tool_dir=str(p.tools.ffmpeg.parent),
        analysis=AnalysisDeployment(enabled=True),audio_topics=deploy,review=p.sought.reviews.config)
    assert not config.precision.enabled
    path=tmp_path/'synthetic-batch-audio.local.json'
    path.write_text(config.model_dump_json(),encoding='utf8')
    executor=executor_from_config(path,e.worker.actor_id)
    # First configured-worker turn actually decodes the selected file via CPU ffprobe.
    assert executor.run_once()['accepted']
    with e.database.transaction() as c:
        probe=c.execute('''SELECT a.envelope_json,a.result_json FROM router_tasks t JOIN router_attempts a
            ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE t.job_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage')='probe' ''',(job_id,)).fetchone()
        assert c.execute("SELECT count(*) FROM router_tasks WHERE job_id=? AND json_extract(spec_json,'$.stage')='audio_topics'",(job_id,)).fetchone()[0]==0
    assert json.loads(probe['envelope_json'])['input_refs'][0]['sha256']==sha256(buffer.getvalue()).hexdigest()
    ref=next(item for item in json.loads(probe['result_json'])['artifacts'] if item['kind']=='technical_report')
    technical=json.loads((e.workspace/ref['ref']).read_text())
    assert technical['kind']=='audio' and technical['duration_seconds']==pytest.approx(.5)

    next_turn=executor.run_once()
    if not auto_dispatch:
        assert next_turn is None
        waiting=app.state.analysis.status(asset.asset_id,principal=e.owner)
        assert waiting['state']=='awaiting_audio_provider' and not waiting['analysis_complete']
        assert not waiting['topic_analysis']['audio_topic_stages']
        assert all(task['stage']=='probe' for task in waiting['runtime']['tasks'])
        pending=client.post('/search/auto-tag/status',json=body,headers=HEADERS).json()
        assert pending['execution_complete'] and pending['waiting']==pending['pending_capability']==1
        assert not pending['semantic_complete'] and pending['pending_fields']==8
        # A further worker poll cannot turn off into on. Explicit HTTP remains usable.
        assert executor.run_once() is None
        current=e.catalog.get_asset(asset.asset_id,principal=e.owner)
        request={'request_id':'explicit-batch-audio','expected_asset_revision':current.revision,
            'source_sha256':current.sha256,'expected_job_revision':waiting['runtime']['revision']}
        explicit=client.post('/analysis/assets/'+asset.asset_id+'/audio-topics',json=request,headers=HEADERS)
        assert explicit.status_code==200 and len(explicit.json()['fields'])==8
        assert executor.run_once()['accepted']
    else:
        assert next_turn['accepted']

    view=app.state.analysis.status(asset.asset_id,principal=e.owner)
    assert view['content_kind']=='audio'
    assert view['state']=='analysis_needs_review'
    assert set(view['topic_analysis']['audio_topic_stages'].values())=={'verified_audio_topics_draft'}
    assert view['topic_analysis']['evidence']
    assert all(evidence['kind']=='audio_segment' and evidence['source_sha256']==asset.sha256
        for evidence in view['topic_analysis']['evidence'])
    with e.database.transaction() as c:
        report=c.execute('''SELECT a.result_json FROM router_tasks t JOIN router_attempts a
            ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch WHERE t.job_id=? AND t.active=1
            AND json_extract(t.spec_json,'$.stage')='audio_topics' ''',(job_id,)).fetchone()
    assert report is not None
    artifacts=json.loads(report[0])['artifacts']
    assert artifacts and all((e.workspace/item['ref']).is_file() for item in artifacts)
    completed=client.post('/search/auto-tag/status',json=body,headers=HEADERS).json()
    assert completed['execution_complete'] and completed['completed']==1 and not completed['semantic_complete']
    assert not completed['medical_confirmation']
    assert not view['medical_confirmation'] and source.read_bytes()==buffer.getvalue()
    assert executor.run_once() is None


def test_batch_export_tracks_complete_snapshot_after_topic_invalidation_and_repair(topic_router,tools):
    t=topic_router;e=t.env
    client,app,body=run_static(t,tools)
    first=client.post('/search/auto-tag/export',json=body,headers=HEADERS)
    assert first.status_code==200
    assert client.post('/search/auto-tag/export',json=body,headers=HEADERS).json()['export_id']==first.json()['export_id']
    with e.database.transaction() as c:
        suggestions_before=[tuple(row) for row in c.execute('SELECT suggestion_id,payload FROM tag_suggestions WHERE asset_id=? ORDER BY suggestion_id',
            (t.sought.asset.asset_id,))]
        row=c.execute('''SELECT a.result_json FROM router_tasks t JOIN router_attempts a
            ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch WHERE t.job_id=? AND t.active=1
            AND json_extract(t.spec_json,'$.stage')='local_topics' ''',
            (app.state.analysis.status(t.sought.asset.asset_id,principal=e.owner)['runtime']['job_id'],)).fetchone()
    raw=next(ref for ref in json.loads(row[0])['artifacts'] if ref['kind']=='local_topic_raw_report')
    (e.workspace/raw['ref']).write_text('{}')
    with e.database.transaction() as c:
        assert [tuple(row) for row in c.execute('SELECT suggestion_id,payload FROM tag_suggestions WHERE asset_id=? ORDER BY suggestion_id',
            (t.sought.asset.asset_id,))]==suggestions_before
    # Invalidation changes qualified values/issues even with unchanged legacy suggestion rows.
    invalidated=client.post('/search/auto-tag/export',json=body,headers=HEADERS)
    assert invalidated.status_code==200,invalidated.text
    assert invalidated.json()['export_id']!=first.json()['export_id']
    assert invalidated.json()['manifest']['snapshot_sha256']!=first.json()['manifest']['snapshot_sha256']
    assert client.post('/search/auto-tag/export',json=body,headers=HEADERS).json()['export_id']==invalidated.json()['export_id']
    assert client.post('/search/auto-tag',json=body,headers=HEADERS).json()['added']==1
    cpu=CPUHandlers(e.router,e.worker,tools)
    executor=LocalExecutor(e.router,e.worker,lambda lease,stopped:
        t.handler(lease,stopped) if lease.task.stage=='local_topics' else cpu(lease,stopped))
    for _ in range(12):
        if executor.run_once() is None:break
    repaired=client.post('/search/auto-tag/export',json=body,headers=HEADERS)
    assert repaired.status_code==200,repaired.text
    assert repaired.json()['export_id'] not in (first.json()['export_id'],invalidated.json()['export_id'])
    assert client.post('/search/auto-tag/export',json=body,headers=HEADERS).json()['export_id']==repaired.json()['export_id']
