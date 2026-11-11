from hashlib import sha256
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from upload.conftest import tools
from .test_router import topic_router, sought, env

from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.intake import ensure_intent
from media_tag.analysis.repair import RepairSubmission, repair
from media_tag.analysis.service import AnalysisService
from media_tag.api.app import create_app
from media_tag.api.config import LocalConfig, executor_from_config
from media_tag.contracts.models import TaskEnvelope
from media_tag.drafts.service import DraftIndex
from media_tag.local_topics.contracts import LocalTopicDeployment
from media_tag.local_topics.runtime import LocalTopicHandlers
from media_tag.router.runtime import CPUHandlers, LocalExecutor, cpu_stages
from media_tag.storage.filesystem import FilesystemBackend


def test_registered_image_dispatches_ten_fields_and_enters_search_csv(topic_router, tools):
    t, e = topic_router, topic_router.env
    e.router.cancel(t.job_id, principal=e.owner)
    e.router.stages.update(cpu_stages(tools))
    e.router.register_worker(e.worker.actor_id, ('probe','evidence','local_topics'), principal=e.admin)
    analysis = AnalysisService(e.router, AnalysisDeployment(enabled=True))
    with e.database.transaction(write=True) as connection:
        ensure_intent(connection,t.sought.asset.asset_id,e.owner.actor_id,content_kind='image')
    analysis.dispatch_pending(principal=e.owner)
    cpu = CPUHandlers(e.router,e.worker,tools)
    executor = LocalExecutor(e.router,e.worker,lambda lease, stopped:
        t.handler(lease,stopped) if lease.task.stage=='local_topics' else cpu(lease,stopped))
    for _ in range(12):
        if executor.run_once() is None:break
    state = analysis.status(t.sought.asset.asset_id,principal=e.owner)
    assert state['topic_analysis']['local_topic_stages']['local_topics']=='verified_local_topic_draft'
    assert not state['analysis_complete'] and not state['medical_confirmation']
    fields = {item['field']:item for item in state['topic_analysis']['fields']}
    assert fields['英文检索词']['value']['values']==['red circle']
    assert fields['过程/状态']['value']['state']=='unknown'
    assert 'jobs/' not in json.dumps(state)
    with e.database.transaction() as connection:
        asset=connection.execute('SELECT * FROM assets WHERE asset_id=?',(t.sought.asset.asset_id,)).fetchone()
        draft=DraftIndex(e.router).sync(connection,asset,persist=False)
    assert any(label['value']=='red circle' and label['origin']=='local' for label in draft['labels'])
    from search.test_query import query
    assert query(t.sought,'red circle',include_drafts=True)['items']
    from topic_index.test_projection import exported
    from types import SimpleNamespace
    result,_,_=exported(SimpleNamespace(env=e,sought=t.sought),include_ai_drafts=True)
    assert result['英文检索词']=='red circle' and result['画面主体']=='合成圆形'
    app=create_app(e.router,{sha256(b'owner').hexdigest():e.owner},analysis_options=analysis.options)
    response=TestClient(app).get('/analysis/assets/'+asset['asset_id'],headers={'Authorization':'Bearer owner'})
    assert response.status_code==200 and response.json()['topic_analysis']['local_topic_stages']


def test_local_report_repair_preserves_probe_and_removes_stale_suggestions(topic_router,tools):
    t,e=topic_router,topic_router.env
    e.router.cancel(t.job_id,principal=e.owner)
    e.router.stages.update(cpu_stages(tools))
    e.router.register_worker(e.worker.actor_id,('probe','evidence','local_topics'),principal=e.admin)
    analysis=AnalysisService(e.router,AnalysisDeployment(enabled=True))
    with e.database.transaction(write=True) as connection:
        ensure_intent(connection,t.sought.asset.asset_id,e.owner.actor_id,content_kind='image')
    analysis.dispatch_pending(principal=e.owner)
    cpu=CPUHandlers(e.router,e.worker,tools)
    executor=LocalExecutor(e.router,e.worker,lambda lease,stopped:t.handler(lease,stopped) if lease.task.stage=='local_topics' else cpu(lease,stopped))
    for _ in range(12):
        if executor.run_once() is None:break
    with e.database.transaction() as connection:
        row=connection.execute("SELECT a.result_json FROM router_tasks t JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch WHERE t.active=1 AND t.step_key='local_topics'").fetchone()
    ref=next(ref for ref in json.loads(row[0])['artifacts'] if ref['kind']=='local_topic_raw_report')
    (e.workspace/ref['ref']).write_text('{}')
    state=analysis.status(t.sought.asset.asset_id,principal=e.owner)
    assert state['topic_analysis']['local_topic_stages']['local_topics']=='unavailable'
    asset=e.catalog.get_asset(t.sought.asset.asset_id,principal=e.owner)
    result=repair(analysis,asset.asset_id,RepairSubmission(request_id='local-repair',expected_asset_revision=asset.revision,
        source_sha256=asset.sha256,expected_job_revision=state['runtime']['revision']),principal=e.owner)
    assert result['patched_roots']==['local_topics'] and 'probe' in result['reused_stage_keys']
    for _ in range(12):
        if executor.run_once() is None:break
    assert analysis.status(asset.asset_id,principal=e.owner)['topic_analysis']['local_topic_stages']['local_topics']=='verified_local_topic_draft'


def test_configured_worker_adopts_unknown_image_without_loading_weights(topic_router,tools,tmp_path,monkeypatch):
    t,e=topic_router,topic_router.env
    e.router.cancel(t.job_id,principal=e.owner)
    monkeypatch.setattr(LocalTopicHandlers,'invoke',t.handler.invoke)
    config=LocalConfig(workspace=str(e.workspace),database=str(e.database.path),object_store=str(e.catalog.object_store.root),
        template_dir=str((Path.cwd()/'asset-index-template').resolve()),template_fingerprint=e.contract.fingerprint,
        credentials={sha256(b'owner').hexdigest():e.owner,sha256(b'worker').hexdigest():e.worker},workers=(e.worker.actor_id,),
        tool_dir=str(tools.ffmpeg.parent),review=t.sought.reviews.config,analysis=AnalysisDeployment(enabled=True),
        local_topics=LocalTopicDeployment(enabled=True,model_root=str(t.handler.registry.root)))
    path=tmp_path/'isolated-local-topics.local.json';path.write_text(config.model_dump_json())
    executor=executor_from_config(path,e.worker.actor_id)
    source=tmp_path/'pipeline-source';source.mkdir();(source/'README.md').write_text('Synthetic source, read only.\n')
    Image.new('RGB',(32,32),'red').save(source/'source.png')
    asset=executor.router.catalog.register_file(FilesystemBackend(source),'source.png',source_backend_id='synthetic-pipeline',
        principal=e.owner,request_id='config-source',name='Unrelated original name')
    for _ in range(12):
        if executor.run_once() is None:break
    state=AnalysisService(executor.router,config.analysis).status(asset.asset_id,principal=e.owner)
    assert state['content_kind']=='image' and state['topic_analysis']['local_topic_stages']['local_topics']=='verified_local_topic_draft'
    assert not state['analysis_complete']


def test_cross_stage_latest_scope_agrees_between_analysis_and_index(topic_router):
    from media_tag.analysis.projection import TopicProjection
    from media_tag.precision.runtime import stage as precision_stage
    from media_tag.precision.contracts import PROMPT, VERSION
    from media_tag.router.models import StepSpec
    from media_tag.contracts.models import Versions
    t,e=topic_router,topic_router.env
    assert t.executor.run_once()['accepted']
    e.router.stages['precision']=precision_stage()
    current=e.router.get_job(t.job_id,principal=e.owner)
    step=StepSpec(key='new-precision',stage='precision',config={'provider_id':'synthetic-unselected',
        'provider_revision':'a'*64,'policy_digest':t.sought.reviews.policy.digest,'content_kind':'image'},
        versions=Versions(rules_version='sha256:'+t.sought.reviews.policy.digest,preprocess_version=VERSION,
            taxonomy_version=t.sought.reviews.policy.config.taxonomy_version,model_revision='a'*64),
        target_fields=('英文检索词',),prompt_version=PROMPT)
    e.router.replan(t.job_id,(step,),expected_revision=current['revision'],principal=e.owner)

    def inspect():
        with e.database.transaction() as connection:
            asset=connection.execute('SELECT * FROM assets WHERE asset_id=?',(t.sought.asset.asset_id,)).fetchone()
            job=connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(t.job_id,)).fetchone()
            document,_=TopicProjection(e.router).build(connection,asset,'image',job)
            draft=DraftIndex(e.router).sync(connection,asset,persist=False)
        return {field.field:field for field in document.fields},draft

    fields,draft=inspect()
    assert fields['英文检索词'].value.state=='unknown'
    assert not any(label.get('field')=='英文检索词' for label in draft['labels'])
    current=e.router.get_job(t.job_id,principal=e.owner)
    e.router.replan(t.job_id,(t.spec,),expected_revision=current['revision'],principal=e.owner)
    fields,draft=inspect()
    assert fields['英文检索词'].value.state=='unknown'
    assert not any(label.get('field')=='英文检索词' for label in draft['labels'])
    # This explicit local retry is newer than the still-pending precision scope.
    assert t.executor.run_once()['accepted']
    fields,draft=inspect()
    assert fields['英文检索词'].origin=='local' and fields['英文检索词'].value.values==('red circle',)
    assert any(label.get('field')=='英文检索词' and label['origin']=='local' for label in draft['labels'])
