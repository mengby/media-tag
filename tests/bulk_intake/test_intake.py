from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import random
from types import SimpleNamespace
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit
from PIL import Image
import pytest
from fastapi.testclient import TestClient
from media_tag.api.app import create_app
from media_tag.catalog import Principal
from media_tag.catalog.service import encode
from media_tag.client.http import ClientSettings
from media_tag.intake.inventory import scan,load_batch
from media_tag.intake.batch import apply,inspect
from media_tag.intake.client import IntakeClient
from media_tag.intake.models import IntakeError


def source(tmp_path,*,noise=False,jfif=False):
    root=tmp_path/'raw';root.mkdir();jobs=tmp_path/'jobs';jobs.mkdir()
    data=BytesIO()
    image=Image.frombytes('RGB',(80,80),random.Random(12).randbytes(80*80*3)) if noise else Image.new('RGB',(24,16),'red')
    image.save(data,'JPEG' if jfif else 'PNG');body=data.getvalue();name='真实原名.jfif' if jfif else '原图.png'
    (root/name).write_bytes(body)
    return root,jobs,name,body


@pytest.fixture
def wire(intake):
    env=intake
    app=create_app(env.router,{sha256(b'owner-token').hexdigest():env.owner,
        sha256(b'reader-token').hexdigest():Principal(actor_id='read-only-test',role='reader')},uploads=env.uploads,web=True)
    with TestClient(app) as api:yield env,api


def client(tmp_path,api,*,role='owner',hook=None):
    path=tmp_path/(role+'-token.local.txt');path.write_text(role+'-token')
    result=IntakeClient(ClientSettings(base_url='http://127.0.0.1:8765',token_file=str(path.absolute())))
    requests=[]
    class Opener:
        def open(self,request,**kwargs):
            parsed=urlsplit(request.full_url);key=parsed.path+('?' + parsed.query if parsed.query else '')
            requests.append((request.method,key))
            response=api.request(request.method,key,content=request.data,headers=dict(request.header_items()))
            if response.status_code>=400:raise HTTPError(request.full_url,response.status_code,'',{},BytesIO(response.content))
            if hook:hook(request,response)
            return BytesIO(response.content)
    result.opener=Opener();result.requests=requests
    return result


def test_inventory_source_untouched_and_same_bytes_reported_as_duplicates(tmp_path):
    root,jobs,name,body=source(tmp_path);(root/'同内容.png').write_bytes(body)
    (root/'.env').write_text('synthetic secret');(root/'旧表.csv').write_text('old,index')
    before={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in root.iterdir()}
    batch,result=scan(root,jobs)
    assert result['ready']==2 and result['unique_sha256']==1 and result['duplicate_paths']==1
    assert result['unsupported']==1 and result['excluded']==1
    _,inventory,_=load_batch(batch);assert len(inventory.entries)==4
    assert before=={p.name:(p.read_bytes(),p.stat().st_mtime_ns) for p in root.iterdir()}
    assert not (root/'README.md').exists()


def test_source_output_overlap_and_manifest_tampering_rejected(tmp_path):
    root,jobs,_,_=source(tmp_path)
    with pytest.raises(IntakeError,match='overlap'):scan(root,root)
    batch,_=scan(root,jobs);value=json.loads((batch/'inventory.json').read_text());value['source_root']=str(tmp_path)
    (batch/'inventory.json').write_text(encode(value))
    with pytest.raises(IntakeError,match='manifest_changed'):load_batch(batch)


def test_editor_intake_resumes_and_repeats_without_new_assets_or_review(tmp_path,wire):
    env,api=wire;root,jobs,name,body=source(tmp_path);batch,_=scan(root,jobs);c=client(tmp_path,api)
    first=apply(batch,c);assert first['state']=='imported' and first['analysis_queued']==1
    row=inspect(batch)['rows'][0];aid=row['asset_id']
    assert api.get('/ui/library/'+aid,headers={'Authorization':'Bearer owner-token'}).json()['active']
    c.requests.clear();second=apply(batch,c)
    assert second['state']=='imported' and all(method=='GET' for method,_ in c.requests)
    other,_=scan(root,jobs);third=apply(other,c);assert third['state']=='imported'
    assert inspect(other)['rows'][0]['asset_id']==aid
    assert (root/name).read_bytes()==body
    with env.database.transaction() as db:
        assert db.execute('SELECT count(*) FROM assets').fetchone()[0]==2
        assert db.execute('SELECT count(*) FROM field_revisions WHERE reason LIKE ?',('%approve%',)).fetchone()[0]==0


def test_process_interrupt_resumes_server_offset_not_local_guess(tmp_path,wire):
    env,api=wire;root,jobs,name,body=source(tmp_path,noise=True);batch,_=scan(root,jobs);interrupted=False
    def hook(request,response):
        nonlocal interrupted
        if request.method=='PUT' and not interrupted:
            interrupted=True;raise KeyboardInterrupt
    c=client(tmp_path,api,hook=hook)
    with pytest.raises(KeyboardInterrupt):apply(batch,c)
    c=client(tmp_path,api);result=apply(batch,c);row=inspect(batch)['rows'][0]
    assert result['state']=='imported' and row['resumed_bytes']==1024
    assert (root/name).read_bytes()==body


def test_lost_chunk_response_replays_the_exact_chunk_idempotently(tmp_path,wire):
    _,api=wire;root,jobs,_,_=source(tmp_path,noise=True);batch,_=scan(root,jobs);lost=False
    def hook(request,response):
        nonlocal lost
        if request.method=='PUT' and not lost:lost=True;raise URLError('synthetic lost response')
    c=client(tmp_path,api,hook=hook);assert apply(batch,c)['state']=='imported';assert c.retry_count==1


def test_changed_source_is_rejected_before_any_upload_and_reader_cannot_import(tmp_path,wire):
    _,api=wire;root,jobs,name,_=source(tmp_path);batch,_=scan(root,jobs);c=client(tmp_path,api)
    (root/name).write_bytes(b'changed')
    result=apply(batch,c);assert result['import_failed']==1
    assert inspect(batch)['rows'][0]['error_code']=='source_changed_since_inventory'
    assert not any(method=='POST' for method,_ in c.requests)
    with pytest.raises(IntakeError,match='editor_required'):apply(batch,client(tmp_path,api,role='reader'))


def test_jfif_keeps_original_filename_bytes_and_is_still_format_checked(tmp_path,wire):
    _,api=wire;root,jobs,name,body=source(tmp_path,jfif=True);batch,_=scan(root,jobs)
    assert apply(batch,client(tmp_path,api))['state']=='imported'
    row=inspect(batch)['rows'][0];detail=api.get('/v1/assets/'+row['asset_id'],headers={'Authorization':'Bearer owner-token'}).json()
    assert detail['name']==name and detail['sha256']==sha256(body).hexdigest()
    assert (root/name).read_bytes()==body


def test_same_sha_different_paths_share_blob_but_preserve_separate_sources(tmp_path,wire):
    env,api=wire;root,jobs,_,body=source(tmp_path);(root/'副本.png').write_bytes(body);batch,_=scan(root,jobs)
    result=apply(batch,client(tmp_path,api));assert result['imported']==2 and result['unique_sha256']==1
    with env.database.transaction() as db:
        assert db.execute('SELECT count(*) FROM blobs WHERE sha256=?',(sha256(body).hexdigest(),)).fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM assets WHERE blob_sha256=?',(sha256(body).hexdigest(),)).fetchone()[0]==2


def test_intake_transport_rejects_arbitrary_paths(tmp_path,wire):
    _,api=wire;c=client(tmp_path,api)
    for path in ('https://example.invalid/uploads','/uploads/../admin','/terminology/active','/ui/library/asset-not-hex'):
        with pytest.raises(IntakeError,match='untrusted_intake_path'):c.call('POST',path,payload={})


def test_misnamed_image_uses_correct_upload_suffix_without_renaming_source(tmp_path,wire):
    _,api=wire;root,jobs,name,body=source(tmp_path,jfif=True)
    wrong=root/'旧文件名.png';(root/name).rename(wrong)
    batch,_=scan(root,jobs);assert apply(batch,client(tmp_path,api))['state']=='imported'
    row=inspect(batch)['rows'][0];assert row['upload_name']=='旧文件名.png.jpg'
    assert wrong.read_bytes()==body and not (root/'旧文件名.png.jpg').exists()


def test_server_pixel_policy_still_blocks_then_new_attempt_uses_updated_budget(tmp_path,wire):
    from media_tag.upload.models import UploadOptions
    env,api=wire;root,jobs,name,body=source(tmp_path);batch,_=scan(root,jobs);c=client(tmp_path,api)
    env.uploads.options=UploadOptions(chunk_bytes=1024,probe_max_pixels=128)
    assert apply(batch,c)['import_failed']==1
    env.uploads.options=UploadOptions(chunk_bytes=1024,probe_max_pixels=512)
    assert apply(batch,c)['state']=='imported'
    row=inspect(batch)['rows'][0]
    assert row['upload_generation']==1 and row['closed_uploads'][0]['state']=='rejected'
    with env.database.transaction() as db:
        task=db.execute("SELECT spec_json FROM router_tasks WHERE job_id=? AND json_extract(spec_json,'$.stage')='probe'",(row['job_id'],)).fetchone()
        assert json.loads(task[0])['config']['max_pixels']==512
    assert (root/name).read_bytes()==body


def test_shared_asset_is_not_withdrawn_or_reenrolled_on_rerun(tmp_path,wire):
    env,api=wire;root,jobs,_,_=source(tmp_path);batch,_=scan(root,jobs);c=client(tmp_path,api);apply(batch,c)
    aid=inspect(batch)['rows'][0]['asset_id']
    env.catalog.change_access(aid,expected_revision=1,subject_id='external-reader',permission='read',principal=env.owner,reason='actual explicit test share')
    assert apply(batch,c)['state']=='imported'
    with env.database.transaction() as db:
        assert db.execute("SELECT count(*) FROM asset_permissions WHERE asset_id=? AND subject_id='external-reader'",(aid,)).fetchone()[0]==1


def test_withdrawn_membership_is_not_silently_restored_by_resume(tmp_path,wire):
    _,api=wire;root,jobs,_,_=source(tmp_path);batch,_=scan(root,jobs);c=client(tmp_path,api);apply(batch,c)
    row=inspect(batch)['rows'][0]
    response=api.post('/ui/library/'+row['asset_id'],headers={'Authorization':'Bearer owner-token'},json={
        'request_id':'explicit-withdraw','expected_revision':1,'output_sha256':row['source_sha256'],'active':False,'recipients':[]})
    assert response.status_code==200
    c.requests.clear();assert apply(batch,c)['import_failed']==1
    assert inspect(batch)['rows'][0]['error_code']=='intake_membership_inactive'
    assert all(method=='GET' for method,_ in c.requests)


def test_processing_enrollment_uses_same_server_probe_budget(intake,tmp_path):
    from media_tag.upload.models import UploadOptions
    env=intake;env.uploads.options=UploadOptions(chunk_bytes=1024,probe_max_pixels=512)
    app=create_app(env.router,{sha256(b'owner-token').hexdigest():env.owner},uploads=env.uploads,web=True)
    root,jobs,name,body=source(tmp_path)
    with TestClient(app) as api:
        auth={'Authorization':'Bearer owner-token'}
        created=api.post('/uploads',headers=auth,json={'request_id':'processing-budget','filename':name,'size_bytes':len(body),
            'sha256':sha256(body).hexdigest(),'purpose':'processing'}).json()
        assert api.put('/uploads/'+created['upload_id']+'/chunks?offset=0',headers=auth|{'X-Chunk-SHA256':sha256(body).hexdigest()},content=body).status_code==200
        completed=api.post('/uploads/'+created['upload_id']+'/complete',headers=auth).json()
        member=api.post('/ui/library/'+completed['asset_id'],headers=auth,json={'request_id':'enroll-processing','expected_revision':1,
            'output_sha256':sha256(body).hexdigest(),'active':True,'recipients':[]})
        assert member.status_code==200
        with env.database.transaction() as db:
            task=db.execute("SELECT spec_json FROM router_tasks WHERE job_id=? AND json_extract(spec_json,'$.stage')='probe'",(member.json()['job_id'],)).fetchone()
            assert json.loads(task[0])['config']['max_pixels']==512


def test_completed_upload_with_pending_dispatch_retries_only_outbox(tmp_path,wire,monkeypatch):
    env,api=wire;root,jobs,_,_=source(tmp_path);batch,_=scan(root,jobs);c=client(tmp_path,api)
    original=env.uploads.dispatch_pending
    monkeypatch.setattr(env.uploads,'dispatch_pending',lambda **kwargs:None)
    first=apply(batch,c);assert first['imported']==1 and first['dispatch_pending']==1
    monkeypatch.setattr(env.uploads,'dispatch_pending',original);c.requests.clear()
    second=apply(batch,c);assert second['state']=='imported' and second['analysis_queued']==1
    assert not any(method=='PUT' for method,_ in c.requests)


def test_limit_advances_past_successful_rows_and_csv_is_safe(tmp_path,wire):
    _,api=wire;root,jobs,name,body=source(tmp_path);(root/'=另一份.png').write_bytes(body)
    batch,_=scan(root,jobs);c=client(tmp_path,api)
    assert apply(batch,c,limit=1)['imported']==1
    assert apply(batch,c,limit=1)['imported']==2
    assert "'=另一份.png" in (batch/'inventory.csv').read_text(encoding='utf-8-sig')
    assert '=另一份.png' in [r['source_key'] for r in inspect(batch)['rows']]
