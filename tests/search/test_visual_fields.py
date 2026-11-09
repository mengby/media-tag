from hashlib import sha256
import io
import csv
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
REAL_POPEN=subprocess.Popen
import pytest
from fastapi.testclient import TestClient
from media_tag.contracts.models import ContractModel,ArtifactRef,Versions,TaskLimits
from media_tag.router.models import StageDefinition,StepSpec,JobSpec
from media_tag.router.runtime import LocalExecutor
from media_tag.semantic.contracts import Caption,FIELDS,FORMS,PREPROCESS
from media_tag.semantic.registry import SemanticRegistry
from media_tag.semantic.runtime import SemanticHandlers,semantic_stage
from media_tag.semantic.service import ensure,SemanticSubmission
from media_tag.workers.evidence import _directory
from media_tag.export.service import ExportService
from media_tag.export.models import ExportSubmission,AssetSelection
from .test_ai_drafts import modeled
from .test_query import query


CAPTION={'subject':'红色圆形几何图','form':'2D插图','keywords_zh':['红色圆形','抽象几何图','白底图形'],
    'keywords_en':['red circle','abstract geometry','white background'],'uncertainties':[]}


@pytest.fixture
def visual(modeled,tmp_path,monkeypatch):
    m=modeled;env=m.env;registry=SemanticRegistry(tmp_path)
    env.router.stages['synthetic_evidence']=StageDefinition(ContractModel,'synthetic-visual-frame-v1',metadata_sensitive=False)
    env.router.stages['visual_fields']=semantic_stage(registry)
    m.sought.search.semantic_registry=registry
    env.router.register_worker(m.worker.actor_id,('synthetic_evidence','visual_fields'),principal=env.admin)
    step=StepSpec(key='evidence',stage='synthetic_evidence',versions=Versions(rules_version='synthetic',preprocess_version='synthetic-frame'))
    job=env.router.submit(JobSpec(asset_id=m.sought.asset.asset_id,steps=(step,)),request_id='visual-parent',principal=env.owner)
    def frame(lease,stopped):
        task=lease.task;folder=_directory(env.workspace,task.output_prefix.rstrip('/'))
        (folder/'README.md').write_text('Synthetic semantic protocol frame; not a real VLM invocation.')
        image=folder/'frame.png';image.write_bytes(m.sought.original.read_bytes())
        ref=ArtifactRef(kind='image',ref=image.relative_to(env.workspace).as_posix(),sha256=sha256(image.read_bytes()).hexdigest())
        manifest=folder/'evidence.json';manifest.write_text(json.dumps({'status':'succeeded','frames':[{'artifact':ref.model_dump(),
            'source_sha256':task.input_refs[0].sha256,'normalized_seconds':None}]}))
        return {'status':'succeeded','artifacts':(ref,ArtifactRef(kind='evidence_manifest',ref=manifest.relative_to(env.workspace).as_posix(),sha256=sha256(manifest.read_bytes()).hexdigest()))}
    assert LocalExecutor(env.router,m.worker,frame).run_once()['accepted']
    options=SimpleNamespace(caption=dict(CAPTION),requests=[])
    class Process:
        returncode=0
        def __init__(self,args,**kwargs):
            request=json.loads(Path(args[-2]).read_text());options.requests.append(request)
            Path(args[-1]).write_text(json.dumps({'ok':True,'data':{'kind':'visual_fields','model_revision':registry.revision,
                'source_sha256':request['source_sha256'],'frame':request['frame'],'prompt_version':request['prompt_version'],
                'caption':options.caption,'raw_text':json.dumps(options.caption),'normalizations':getattr(options,'normalizations',[]),'synthetic_test_only':True}}))
        def poll(self):return 0
        def wait(self):return 0
        def kill(self):raise AssertionError('already stopped')
    monkeypatch.setattr('media_tag.semantic.runtime.subprocess.Popen',Process)
    spec=SemanticSubmission(job_id=job['job_id'],expected_revision=m.sought.asset.revision,source_sha256=m.sought.asset.sha256)
    def add(actor=None):return ensure(m.sought.search,m.sought.asset.asset_id,spec,principal=actor or env.owner)
    executor=LocalExecutor(env.router,m.worker,SemanticHandlers(env.router,registry))
    return SimpleNamespace(m=m,env=env,registry=registry,job=job,spec=spec,add=add,executor=executor,options=options)


def test_visual_fields_reuse_evidence_preserve_human_values_and_enter_search_export(visual):
    v=visual;m=v.m;before=v.env.catalog.get_asset(m.sought.asset.asset_id,principal=v.env.owner)
    first=v.add();assert not first['reused'] and v.add()['reused']
    assert v.executor.run_once()['accepted']
    assert v.add()['reused']
    after=v.env.catalog.get_asset(before.asset_id,principal=v.env.owner)
    assert after.revision==before.revision and after.effective_values==before.effective_values
    assert len(after.suggestions)==4 and {s.field for s in after.suggestions}==set(FIELDS)
    draft=m.sync();assert draft['stages']['visual_fields']=='ready'
    assert len([l for l in draft['labels'] if l['kind']=='field_suggestion'])==4
    assert query(m.sought,'抽象几何图',include_drafts=True)['items']
    assert not query(m.sought,'抽象几何图')['items']
    assert not query(m.sought,'抽象几何图',include_drafts=True,actor=v.env.other)['items']
    assert all('filename' not in request and 'name' not in request for request in v.options.requests)
    exports=ExportService(m.sought.reviews)
    result=exports.create(ExportSubmission(request_id='semantic-formal',assets=(AssetSelection(asset_id=before.asset_id,
        expected_revision=before.revision,source_sha256=before.sha256),),include_ai_drafts=True),principal=v.env.owner)
    rows=list(csv.DictReader(io.StringIO(exports.content(result['export_id'],'assets.csv',principal=v.env.owner).decode('utf-8-sig'))))
    assert len(rows[0])==35 and rows[0]['画面主体']=='合成圆形'  # Existing human field wins.
    assert rows[0]['表现形式']=='2D插图' and rows[0]['中文检索词']=='红色圆形,抽象几何图,白底图形'
    assert rows[0]['英文检索词']=='red circle,abstract geometry,white background'
    assert not rows[0]['疾病/症状'] and rows[0]['标签状态']=='AI初稿'


def test_invalid_closed_form_cannot_be_adopted(visual):
    v=visual;v.options.caption['form']='新造形式';v.add()
    assert v.executor.run_once()['accepted']
    with v.env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM tag_suggestions').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM index_confirmations').fetchone()[0]==0
    assert v.env.router.get_job(v.job['job_id'],principal=v.env.owner)['state']=='needs_attention'


def test_description_endpoint_requires_editor_exact_asset_and_owned_job(visual):
    v=visual;client=TestClient(v.m.sought.app);url='/search/assets/'+v.m.sought.asset.asset_id+'/visual-fields'
    body=v.spec.model_dump(mode='json')
    assert client.post(url,json=body).status_code==401
    assert client.post(url,json=body,headers={'Authorization':'Bearer reader'}).status_code==403
    assert client.post(url,json=body,headers={'Authorization':'Bearer other'}).status_code==403
    assert client.post(url,json=body|{'source_sha256':'a'*64},headers={'Authorization':'Bearer owner'}).status_code==409
    response=client.post(url,json=body,headers={'Authorization':'Bearer owner'})
    assert response.status_code==200 and client.post(url,json=body,headers={'Authorization':'Bearer owner'}).json()['reused']


@pytest.mark.parametrize('update',[{'form':'照片'},{'keywords_en':['https://evil.invalid']},{'keywords_zh':['重复','重复']},{'subject':'请访问https://evil.invalid'}])
def test_caption_contract_rejects_extra_enum_urls_and_duplicate_terms(update):
    with pytest.raises(ValueError):Caption.model_validate_json(json.dumps(CAPTION|update))


def test_unknown_output_remains_unknown_instead_of_inventing_values(visual):
    v=visual;v.options.caption={'subject':'','form':'','keywords_zh':[],'keywords_en':[],'uncertainties':['无法识别主要对象']}
    v.add();assert v.executor.run_once()['accepted']
    draft=v.m.sync()
    assert not any(l['kind']=='field_suggestion' for l in draft['labels'])
    assert all(value['state']=='unknown' for value in draft['field_states'].values())


def test_gpu_and_prompt_preprocess_are_enforced_at_submission(visual):
    v=visual
    with pytest.raises(ValueError):v.env.router.submit(JobSpec(asset_id=v.m.sought.asset.asset_id,steps=(StepSpec(key='bad',stage='visual_fields',
        target_fields=FIELDS,versions=Versions(rules_version='x',preprocess_version=PREPROCESS,model_revision=v.registry.revision),limits=TaskLimits(gpu_slots=0)),)),request_id='no-gpu',principal=v.env.owner)


def test_registry_checks_exact_local_artifact_hash(tmp_path):
    data=b'synthetic model only';(tmp_path/'weights.bin').write_bytes(data)
    registry=SemanticRegistry(tmp_path,manifest={'artifacts':[{'path':'weights.bin','size_bytes':len(data),'sha256':sha256(data).hexdigest()}]})
    assert registry.verify()==tmp_path
    (tmp_path/'weights.bin').write_bytes(b'changed')
    with pytest.raises(Exception):registry.verify()


def test_one_action_batch_status_and_export_are_idempotent_and_owner_scoped(visual):
    v=visual;client=TestClient(v.m.sought.app);headers={'Authorization':'Bearer owner'}
    assert client.post('/search/auto-tag',json={},headers={'Authorization':'Bearer reader'}).status_code==403
    queued=client.post('/search/auto-tag',json={},headers=headers)
    assert queued.status_code==200 and queued.json()['added']==1 and queued.json()['errors']==[]
    assert client.post('/search/auto-tag',json={},headers=headers).json()['reused']==1
    assert client.post('/search/auto-tag/export',json={},headers=headers).status_code==409
    assert v.executor.run_once()['accepted']
    status=client.post('/search/auto-tag/status',json={},headers=headers).json()
    assert status['state']=='ready' and status['completed']==status['total']==1
    exported=client.post('/search/auto-tag/export',json={},headers=headers)
    assert exported.status_code==200 and exported.json()['assets_url'].endswith('/assets.csv')
    assert client.post('/search/auto-tag/export',json={},headers=headers).json()['export_id']==exported.json()['export_id']


def test_parser_keeps_usable_fields_but_does_not_map_unknown_closed_options():
    from media_tag.semantic.contracts import parse_caption
    caption,changes=parse_caption(json.dumps(CAPTION|{'form':'照片','keywords_en':['3D illustration','red circle','red circle','https://evil.invalid']}))
    assert caption.form=='' and caption.subject==CAPTION['subject']
    assert caption.keywords_en==('3D illustration','red circle')
    assert 'form_outside_template_left_unknown' in changes


def test_batch_does_not_export_until_other_analysis_stages_finish(visual):
    v=visual;v.add();assert v.executor.run_once()['accepted']
    job=v.env.router.get_job(v.job['job_id'],principal=v.env.owner)
    v.env.router.replan(v.job['job_id'],(StepSpec(key='extra_observation',stage='synthetic_evidence',
        versions=Versions(rules_version='synthetic',preprocess_version='synthetic-frame')),),
        expected_revision=job['revision'],principal=v.env.owner)
    client=TestClient(v.m.sought.app);headers={'Authorization':'Bearer owner'}
    state=client.post('/search/auto-tag/status',json={},headers=headers).json()
    assert state['state']=='running' and state['completed']==0 and state['running']==1
    assert client.post('/search/auto-tag/export',json={},headers=headers).status_code==409
    def finish(lease,stopped):
        folder=_directory(v.env.workspace,lease.task.output_prefix.rstrip('/'))
        (folder/'README.md').write_text('Synthetic extra observation.')
        path=folder/'result.json';path.write_text('{}')
        return {'status':'succeeded','artifacts':(ArtifactRef(kind='test_report',ref=path.relative_to(v.env.workspace).as_posix(),sha256=sha256(path.read_bytes()).hexdigest()),)}
    assert LocalExecutor(v.env.router,v.m.worker,finish).run_once()['accepted']
    assert client.post('/search/auto-tag/status',json={},headers=headers).json()['state']=='ready'


def test_missing_classification_vocabulary_does_not_block_completed_visual_drafts(visual):
    v=visual;v.add();assert v.executor.run_once()['accepted']
    job=v.env.router.get_job(v.job['job_id'],principal=v.env.owner)
    v.env.router.register_worker(v.m.worker.actor_id,('synthetic_evidence','visual_fields','clip'),principal=v.env.admin)
    v.env.router.replan(v.job['job_id'],(StepSpec(key='clip',stage='clip',depends_on=('evidence',),
        versions=Versions(rules_version='synthetic',model_revision=v.m.revision,preprocess_version='synthetic-frames')),),
        expected_revision=job['revision'],principal=v.env.owner)
    assert LocalExecutor(v.env.router,v.m.worker,v.m.handler).run_once()['accepted']
    client=TestClient(v.m.sought.app)
    state=client.post('/search/auto-tag/status',json={},headers={'Authorization':'Bearer owner'}).json()
    assert state['state']=='ready' and state['completed']==1


def test_actual_processor_caps_pixels_and_keeps_batch_images_distinct():
    from PIL import Image
    from media_tag.models.native import prepare_native_runtime
    from media_tag.semantic.processor import make_processor
    root=Path('cache/models/semantic')
    if not (root/'tokenizer.json').exists():pytest.skip('explicitly downloaded semantic processor not installed')
    prepare_native_runtime();pytest.importorskip('torchvision')
    processor=make_processor(root,512**2)
    messages=[[{'role':'user','content':[{'type':'image','image':Image.new('RGB',(960,960),color)},
        {'type':'text','text':'只描述可见形状'}]}] for color in ('red','blue')]
    inputs=processor.apply_chat_template(messages,tokenize=True,add_generation_prompt=True,padding=True,return_dict=True,return_tensors='pt')
    grids=inputs['image_grid_thw'].tolist();assert len(grids)==2
    assert all(h*w*16**2<=512**2 for _,h,w in grids)
    count=grids[0][0]*grids[0][1]*grids[0][2]
    assert not inputs['pixel_values'][:count].equal(inputs['pixel_values'][count:])


def test_keyword_stopping_requires_three_complete_distinct_phrases():
    from media_tag.semantic.stopping import complete
    assert not complete(2,'红色,圆形,白')
    assert complete(2,'红色,圆形,白底,')
    assert not complete(3,'person,person,person,')
    assert complete(3,'person,stomach,glowing,')
    assert complete(1,'剖面') and not complete(1,'剖')


@pytest.mark.parametrize('bias',[True,False])
def test_full_patch_linear_is_numerically_equivalent_to_conv3d(bias):
    from media_tag.models.native import prepare_native_runtime
    prepare_native_runtime();torch=pytest.importorskip('torch')
    from media_tag.semantic.patch_projection import replace_patch_projection
    torch.manual_seed(7);torch.set_num_threads(1)
    conv=torch.nn.Conv3d(3,32,kernel_size=(2,4,4),stride=(2,4,4),bias=bias)
    values=torch.randn(9,3,2,4,4)
    expected=conv(values)
    model=SimpleNamespace(model=SimpleNamespace(visual=SimpleNamespace(patch_embed=SimpleNamespace(proj=conv))))
    assert replace_patch_projection(model)=='full-patch-linear-v1'
    actual=model.model.visual.patch_embed.proj(values)
    torch.testing.assert_close(actual,expected,rtol=1e-5,atol=1e-6)
    assert model.model.visual.patch_embed.proj.weight is conv.weight


def test_single_cli_batch_action_waits_indexes_and_exports(visual,tmp_path):
    from media_tag.intake.inventory import scan,load_batch
    from media_tag.intake.tag import run
    from media_tag.deployment.local import write_json
    v=visual;client=TestClient(v.m.sought.app);jobs=tmp_path/'jobs';jobs.mkdir()
    batch,_=scan(v.m.sought.original.parent,jobs);_,inventory,progress=load_batch(batch)
    progress.update(state='imported',actor_id=v.env.owner.actor_id,service_origin='http://127.0.0.1:8765',
        rows={r.key:{'state':'imported','asset_id':v.m.sought.asset.asset_id,'job_id':v.job['job_id']} for r in inventory.entries if r.state=='ready'})
    write_json(batch,'progress.json',progress)
    class Client:
        origin='http://127.0.0.1:8765'
        def editor(self):return v.env.owner.actor_id
        def call(self,method,path,*,payload=None):
            if path=='/search/auto-tag/status':v.executor.run_once()
            response=client.request(method,path,json=payload,headers={'Authorization':'Bearer owner'});assert response.status_code==200,response.text
            return response.json()
        def export_content(self,identifier,name):
            response=client.get('/review/exports/'+identifier+'/content/'+name,headers={'Authorization':'Bearer owner'});assert response.status_code==200
            return response.content
    report=run(batch,Client());assert report['state']=='ready' and report['field_drafts']==dict.fromkeys(FIELDS,1)
    assert (batch/'auto-tag-result.json').exists()


def test_real_browser_single_action_to_export_with_isolated_synthetic_worker(visual,tmp_path):
    import threading,time
    from terminology.lexicon.validate_http import server
    chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    playwright=Path('C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')
    if not chrome.exists() or not playwright.exists():pytest.skip('explicit local browser runtime unavailable')
    v=visual;stopped=threading.Event();failures=[]
    def work():
        try:
            while not stopped.wait(.1):v.executor.run_once()
        except Exception as exc:failures.append(type(exc).__name__)
    worker=threading.Thread(target=work,daemon=True);worker.start()
    try:
        with server(v.m.sought.app) as origin:
            config=tmp_path/'browser-context.local.json';config.write_text(json.dumps({'base_url':origin,'chrome':str(chrome),'playwright':str(playwright),'output_dir':str(tmp_path)}))
            process=REAL_POPEN(['node',str(Path('tests/search/auto-tag-browser.mjs').resolve()),str(config)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            stdout,stderr=process.communicate(timeout=90)
            assert process.returncode==0,stderr.decode(errors='replace')
            assert json.loads((tmp_path/'auto-tag-browser.json').read_text())['state']=='passed'
    finally:stopped.set();worker.join(10)
    assert not failures
    with v.env.database.transaction() as c:
        assert c.execute("SELECT count(*) FROM router_tasks WHERE json_extract(spec_json,'$.stage')='visual_fields'").fetchone()[0]==1


@pytest.mark.parametrize('same_owner,force_oom',[(True,False),(False,False),(True,True)])
def test_microbatch_cache_keeps_asset_binding_and_never_crosses_owner(visual,tmp_path,monkeypatch,same_owner,force_oom):
    from PIL import Image
    from media_tag.storage.filesystem import FilesystemBackend
    v=visual;owner=v.env.owner if same_owner else v.env.other
    sources=tmp_path/'blue-source';sources.mkdir();Image.new('RGB',(24,24),'blue').save(sources/'blue.png')
    asset=v.env.catalog.register_file(FilesystemBackend(sources),'blue.png',source_backend_id='synthetic-blue',principal=owner,request_id='blue',name='unrelated-blue-name')
    step=StepSpec(key='evidence',stage='synthetic_evidence',versions=Versions(rules_version='synthetic',preprocess_version='synthetic-frame'))
    job=v.env.router.submit(JobSpec(asset_id=asset.asset_id,steps=(step,)),request_id='blue-parent',principal=owner)
    def evidence(lease,stopped):
        folder=_directory(v.env.workspace,lease.task.output_prefix.rstrip('/'));(folder/'README.md').write_text('Synthetic blue frame.')
        image=folder/'frame.png';image.write_bytes((sources/'blue.png').read_bytes())
        ref=ArtifactRef(kind='image',ref=image.relative_to(v.env.workspace).as_posix(),sha256=sha256(image.read_bytes()).hexdigest())
        manifest=folder/'evidence.json';manifest.write_text(json.dumps({'status':'succeeded','frames':[{'artifact':ref.model_dump(),
            'source_sha256':lease.task.input_refs[0].sha256,'normalized_seconds':None}]}))
        return {'status':'succeeded','artifacts':(ref,ArtifactRef(kind='evidence_manifest',ref=manifest.relative_to(v.env.workspace).as_posix(),sha256=sha256(manifest.read_bytes()).hexdigest()))}
    assert LocalExecutor(v.env.router,v.m.worker,evidence).run_once()['accepted']
    v.add();ensure(v.m.sought.search,asset.asset_id,SemanticSubmission(job_id=job['job_id'],source_sha256=asset.sha256,expected_revision=asset.revision),principal=owner)
    calls=[];handler=SemanticHandlers(v.env.router,v.registry,batch_size=2)
    def invoke(request,output,task,stopped,**kwargs):
        calls.append(json.loads(json.dumps(request)))
        if force_oom and request.get('prefetch'):return {'ok':False,'error_code':'oom'}
        def report(item):
            caption=dict(CAPTION)
            if item['source_sha256']==asset.sha256:caption['subject']='蓝色几何图'
            return {'kind':'visual_fields','model_revision':v.registry.revision,'prompt_version':request['prompt_version'],
                'source_sha256':item['source_sha256'],'frame':item['frame'],'caption':caption,'synthetic_test_only':True}
        return {'ok':True,'data':report(request),'prefetched':[{'task_id':item['task_id'],'report':report(item)} for item in request.get('prefetch',[])]}
    monkeypatch.setattr(handler,'invoke',invoke)
    executor=LocalExecutor(v.env.router,v.m.worker,handler)
    assert executor.run_once()['accepted'] and executor.run_once()['accepted']
    assert len(calls)==(3 if force_oom else 1 if same_owner else 2)
    if force_oom:assert handler.batch_size==1
    blue=v.env.catalog.get_asset(asset.asset_id,principal=owner)
    assert next(s.value.values[0] for s in blue.suggestions if s.field=='画面主体')=='蓝色几何图'
    assert all(not request.get('prefetch') for request in calls) if not same_owner else calls[0]['prefetch']


def test_cancelled_native_process_stops_before_gpu_lease_is_released(visual,monkeypatch):
    import sys,threading
    from media_tag.models.contracts import ModelFailure
    v=visual;v.add();lease=v.env.router.claim(principal=v.m.worker);processes=[];stopped=threading.Event()
    def native(args,**kwargs):
        process=REAL_POPEN([sys.executable,'-c','import time;time.sleep(30)'],**kwargs)
        processes.append(process);stopped.set();return process
    monkeypatch.setattr('media_tag.semantic.runtime.subprocess.Popen',native)
    with pytest.raises(ModelFailure,match='cancelled'):SemanticHandlers(v.env.router,v.registry)(lease,stopped)
    assert processes and processes[0].poll() is not None
    v.env.router.release(lease.task.task_id,lease.task.lease_epoch,principal=v.m.worker)
    with v.env.database.transaction() as c:assert c.execute('SELECT count(*) FROM router_holds').fetchone()[0]==0


def test_unmatched_form_is_distinct_from_missing_information_in_draft_view(visual):
    v=visual;v.options.caption['form']='';v.options.normalizations=['form_outside_template_left_unknown']
    v.add();assert v.executor.run_once()['accepted']
    draft=v.m.sync();assert draft['field_states']['表现形式']['state']=='no_match'
    assert 'form_outside_template_left_unknown' in draft['normalizations']
