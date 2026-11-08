from hashlib import sha256
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from media_tag.catalog import Principal
from media_tag.contracts.models import ContractModel,Versions,ArtifactRef,Evidence,ErrorDetail
from media_tag.router.models import StageDefinition,JobSpec,StepSpec
from media_tag.router.runtime import LocalExecutor
from media_tag.workers.evidence import _directory
from media_tag.drafts.service import DraftIndex
from media_tag.export.service import ExportService
from media_tag.export.models import ExportSubmission,AssetSelection
from .test_query import query


@pytest.fixture
def modeled(sought):
    env=sought.env;revision='synthetic-visual-v1';reports={}
    with env.database.transaction(write=True) as c:c.execute('UPDATE router_jobs SET cancelled=1 WHERE asset_id=?',(sought.asset.asset_id,))
    for stage in ('sample','ocr','face','clip'):env.router.stages[stage]=StageDefinition(ContractModel,'synthetic-model-v1',metadata_sensitive=False)
    worker=Principal(actor_id='draft-worker',role='worker')
    env.router.register_worker(worker.actor_id,('sample','ocr','face','clip'),principal=env.admin)
    versions=Versions(rules_version='synthetic',model_revision=revision,preprocess_version='synthetic-frames')
    steps=(StepSpec(key='sample',stage='sample',versions=versions),)+tuple(StepSpec(key=stage,stage=stage,depends_on=('sample',),versions=versions) for stage in ('ocr','face','clip'))
    job=env.router.submit(JobSpec(asset_id=sought.asset.asset_id,steps=steps),request_id='model-drafts',principal=env.owner)
    def handle(lease,stopped):
        task=lease.task;source=task.input_refs[0]
        if task.stage=='sample':
            folder=_directory(env.router.workspace,task.output_prefix.rstrip('/'));(folder/'README.md').write_text('Synthetic frame.')
            path=folder/'frame.png';path.write_bytes(sought.original.read_bytes())
            return {'status':'succeeded','artifacts':(ArtifactRef(kind='image',ref=path.relative_to(env.router.workspace).as_posix(),sha256=sha256(path.read_bytes()).hexdigest()),)}
        frame=next(r for r in task.input_refs if r.kind=='image')
        observation={'ocr':{'state':'observed','lines':[{'text':'唯一画中文字','recognition_score':.9,'quadrilateral':[[0,0],[10,0],[10,10],[0,10]]}]},
            'face':{'state':'observed','faces':[{'box_xyxy':[1,1,10,10],'detection_score':.8}]},
            'clip':{'embedding':[1.]+[0.]*511}}[task.stage]
        report={'kind':task.stage,'source_sha256':source.sha256,'model_revision':revision,
            'candidate_state':'missing_published_vocabulary','frames':[{'artifact':frame.model_dump(),'normalized_seconds':None,'observation':observation}]}
        folder=_directory(env.router.workspace,task.output_prefix.rstrip('/'));(folder/'README.md').write_text('Synthetic accepted model projection test.')
        path=folder/'model-report.json';path.write_text(json.dumps(report),encoding='utf8');reports[task.stage]=path
        ref=ArtifactRef(kind='model_report',ref=path.relative_to(env.router.workspace).as_posix(),sha256=sha256(path.read_bytes()).hexdigest())
        return {'status':'partial' if task.stage=='clip' else 'succeeded','artifacts':(ref,),
            'evidence':(Evidence(evidence_id='sample-frame',kind='image',source_sha256=source.sha256,artifact=frame),),
            'errors':(ErrorDetail(code='unpublished_vocabulary',message='Synthetic missing vocabulary'),) if task.stage=='clip' else ()}
    executor=LocalExecutor(env.router,worker,handle)
    assert all(executor.run_once()['accepted'] for _ in steps)
    index=DraftIndex(env.router)
    def sync():
        with env.database.transaction(write=True) as c:return index.sync(c,env.catalog._authorize(c,sought.asset.asset_id,env.owner))
    return SimpleNamespace(sought=sought,env=env,job=job,reports=reports,sync=sync,revision=revision,handler=handle,worker=worker)


def test_current_models_materialize_evidence_bound_idempotent_drafts_without_human_changes(modeled):
    m=modeled;before=m.env.catalog.get_asset(m.sought.asset.asset_id,principal=m.env.owner)
    first=m.sync();second=m.sync()
    assert first==second and first['stages']['ocr']=='ready' and first['stages']['clip']=='visual_features_ready_classification_missing'
    assert len(first['labels'])==2 and len(first['features'])==1
    assert all(l['state']=='ai_draft' and l['report_sha256'] and l['frame_sha256'] for l in first['labels'])
    assert all(not l['medical_confirmation'] for l in first['labels'])
    assert m.env.catalog.get_asset(before.asset_id,principal=m.env.owner)==before
    with m.env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_draft_documents').fetchone()[0]==3
        assert c.execute('SELECT count(*) FROM tag_suggestions').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM index_confirmations').fetchone()[0]==0
    assert not query(m.sought,'唯一画中文字')['items']
    found=query(m.sought,'唯一画中文字',include_drafts=True)['items'][0]['versions'][0]
    assert all(r['kind']=='ai_draft' and not r['medical_evidence'] for r in found['reasons'])
    assert not query(m.sought,'唯一画中文字',include_drafts=True,actor=m.env.other)['items']
    assert query(m.sought,'检测到人脸',include_drafts=True)['items']


def test_reports_and_frames_are_reverified_and_cancellation_invalidates_cache(modeled):
    m=modeled;m.sync();m.reports['ocr'].write_bytes(b'changed')
    assert m.sync()['stages']['ocr']=='unavailable'
    assert not query(m.sought,'唯一画中文字',include_drafts=True)['items']
    with m.env.database.transaction(write=True) as c:c.execute('UPDATE router_jobs SET cancelled=1 WHERE job_id=?',(m.job['job_id'],))
    result=m.sync();assert not result['labels'] and not result['features']
    with m.env.database.transaction() as c:assert c.execute('SELECT count(*) FROM ai_draft_documents').fetchone()[0]==0


def test_input_frame_tamper_is_not_hidden_by_persisted_drafts(modeled):
    m=modeled;m.sync()
    report=json.loads(m.reports['ocr'].read_text())
    frame=m.env.router.workspace/report['frames'][0]['artifact']['ref'];frame.write_bytes(b'changed frame')
    result=m.sync()
    assert not result['labels'] and not result['features']
    assert set(result['stages'].values())=={'unavailable'}


def test_new_pending_model_result_does_not_fall_back_to_old_success(modeled):
    m=modeled;m.sync()
    step=StepSpec(key='ocr',stage='ocr',versions=Versions(rules_version='new',model_revision=m.revision,preprocess_version='synthetic-frames'))
    m.env.router.submit(JobSpec(asset_id=m.sought.asset.asset_id,steps=(step,)),request_id='new-source-observation',principal=m.env.owner)
    assert m.sync()['stages']['ocr']=='queued'
    assert not query(m.sought,'唯一画中文字',include_drafts=True)['items']


def test_search_evidence_checks_do_not_hold_sqlite_writer_lock(modeled,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    m=modeled;m.sync();entered=threading.Event();written=threading.Event()
    real=m.sought.search.drafts.sync
    def slow(*args,**kwargs):
        entered.set();assert written.wait(3)
        return real(*args,**kwargs)
    monkeypatch.setattr(m.sought.search.drafts,'sync',slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(query,m.sought,'唯一画中文字',include_drafts=True)
        assert entered.wait(3)
        try:
            with m.env.database.transaction(write=True) as c:c.execute("INSERT INTO catalog_metadata VALUES('concurrent-worker','ok')")
        finally:written.set()
        assert pending.result()['items']


def test_ocr_ledger_escapes_formula_after_invisible_prefix():
    import csv,io
    from media_tag.intake.inventory import csv_bytes
    raw=csv_bytes([{'text':'\u200b=HYPERLINK("https://example.invalid")'}],('text',))
    value=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))[0]['text']
    assert value.startswith("'\u200b=")


def test_configured_worker_refreshes_index_after_actual_lease_release(modeled,tmp_path,monkeypatch):
    from media_tag.api import config as module
    m=modeled
    config={'workspace':str(m.env.router.workspace),'database':str(m.env.database.path),
        'object_store':str(m.env.catalog.object_store.root),'template_dir':str(tmp_path),
        'template_fingerprint':m.env.contract.fingerprint,'credentials':{'a'*64:m.worker.model_dump(mode='json')},
        'workers':[m.worker.actor_id],'search':{'enabled':True}}
    path=tmp_path/'runtime.local.json';path.write_text(json.dumps(config))
    monkeypatch.setattr(module,'load_application',lambda path:(m.sought.app,m.env.router,None))
    monkeypatch.setattr(module,'ModelHandlers',lambda *args,**kwargs:m.handler)
    executor=module.executor_from_config(path,m.worker.actor_id)
    versions=Versions(rules_version='new-hook',model_revision=m.revision,preprocess_version='synthetic-frames')
    m.env.router.submit(JobSpec(asset_id=m.sought.asset.asset_id,steps=(StepSpec(key='sample',stage='sample',versions=versions),
        StepSpec(key='face',stage='face',depends_on=('sample',),versions=versions))),request_id='auto-index',principal=m.env.owner)
    assert executor.run_once()['accepted'] and executor.run_once()['accepted']
    with m.env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM ai_draft_documents').fetchone()[0]==3
        payload=json.loads(c.execute("SELECT payload_json FROM ai_draft_documents WHERE stage='face'").fetchone()[0])
        assert payload['labels'][0]['text']=='检测到人脸，需核查打码'


def test_visual_search_is_explicit_model_bound_and_reports_uncalibrated_score(modeled):
    m=modeled
    class Encoder:
        registry=SimpleNamespace(revision=m.revision)
        def encode(self,text):return [1.]+[0.]*511
    m.sought.search.visual_encoder=Encoder()
    assert not query(m.sought,'新视觉问题',include_visual_drafts=True)['items']
    result=query(m.sought,'新视觉问题',include_drafts=True,include_visual_drafts=True)
    reason=result['items'][0]['versions'][0]['reasons'][0]
    assert reason['kind']=='ai_visual_draft' and reason['visual_similarity']==1 and not reason['medical_evidence']
    assert reason['score_kind']=='cosine_not_probability' and result['visual_drafts']['state']=='ready_uncalibrated'
    assert not query(m.sought,'没有人脸',include_drafts=True,include_visual_drafts=True)['items']
    Encoder.registry=SimpleNamespace(revision='different')
    assert not query(m.sought,'新视觉问题',include_drafts=True,include_visual_drafts=True)['items']


def test_draft_detail_permissions_and_formal_export_preserve_template_and_human_values(modeled):
    m=modeled;client=TestClient(m.sought.app);identifier=m.sought.asset.asset_id
    url='/search/assets/'+identifier+'/ai-draft'
    assert client.get(url).status_code==401
    assert client.get(url,headers={'Authorization':'Bearer other'}).status_code==403
    response=client.get(url,headers={'Authorization':'Bearer reader'})
    assert response.status_code==200 and response.json()['visual_features']['frames']==1
    assert 'features' not in response.json()
    exports=ExportService(m.sought.reviews)
    asset=m.env.catalog.get_asset(identifier,principal=m.env.owner)
    spec=ExportSubmission(request_id='ai-template',assets=(AssetSelection(asset_id=identifier,expected_revision=asset.revision,source_sha256=asset.sha256),),include_ai_drafts=True)
    result=exports.create(spec,principal=m.env.owner)
    import csv,io
    rows=list(csv.DictReader(io.StringIO(exports.content(result['export_id'],'assets.csv',principal=m.env.owner).decode('utf-8-sig'))))
    assert len(rows[0])==35 and rows[0]['标签状态']=='AI初稿' and rows[0]['含人脸']=='有-需打码'
    assert rows[0]['画面主体']=='合成圆形' and not rows[0]['疾病/症状'] and rows[0]['许可状态']=='未核实'


def test_batch_drafts_downloads_real_formal_contract_and_reuses_snapshot(modeled,tmp_path):
    from media_tag.intake.inventory import scan,load_batch
    from media_tag.intake.drafts import materialize
    from media_tag.deployment.local import write_json
    m=modeled;client=TestClient(m.sought.app);jobs=tmp_path/'batch-jobs';jobs.mkdir()
    batch,_=scan(m.sought.original.parent,jobs)
    _,inventory,progress=load_batch(batch)
    progress.update(state='imported',actor_id=m.env.owner.actor_id,service_origin='http://127.0.0.1:8765',
        rows={r.key:{'asset_id':m.sought.asset.asset_id,'job_id':m.job['job_id'],'state':'imported'} for r in inventory.entries if r.state=='ready'})
    write_json(batch,'progress.json',progress)
    class Client:
        origin='http://127.0.0.1:8765'
        def editor(self):return m.env.owner.actor_id
        def call(self,method,path,*,payload=None):
            response=client.request(method,path,json=payload,headers={'Authorization':'Bearer owner'})
            assert response.status_code==200,response.text
            return response.json()
        def export_content(self,identifier,name):
            response=client.get('/review/exports/'+identifier+'/content/'+name,headers={'Authorization':'Bearer owner'})
            assert response.status_code==200
            return response.content
    first=materialize(batch,Client());second=materialize(batch,Client())
    assert first==second and first['state']=='ready' and first['asset_rows']==1 and first['formal_columns']==35
    assert first['ocr_labels']==1 and first['face_detected_assets']==1 and first['visual_indexed_assets']==1
    assert (batch/'ai-drafts-result.json').exists()


def test_additive_v10_migration_rolls_back_and_retains_v9_data(tmp_path,monkeypatch):
    import media_tag.catalog.database as module
    database=module.Database(tmp_path/'old.sqlite3')
    original=module.DRAFT_SCHEMA;monkeypatch.setattr(module,'DRAFT_SCHEMA',())
    # Construct the exact v9 predecessor, without changing its checksums.
    database.initialize()
    with database.transaction(write=True) as c:
        c.execute('DELETE FROM schema_migrations WHERE version=10');c.execute('PRAGMA user_version=9')
        c.execute("INSERT INTO catalog_metadata VALUES('retained','value')")
    monkeypatch.setattr(module,'DRAFT_SCHEMA',(*original,'invalid synthetic SQL'))
    with pytest.raises(Exception):database.initialize()
    with database.transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==9
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='ai_draft_documents'").fetchone()
    monkeypatch.setattr(module,'DRAFT_SCHEMA',original);database.initialize();database.initialize()
    with database.transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==10
        assert c.execute("SELECT value FROM catalog_metadata WHERE key='retained'").fetchone()[0]=='value'
