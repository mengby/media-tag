from copy import deepcopy
from datetime import datetime,timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlsplit
import pytest
from fastapi.testclient import TestClient
from media_tag.catalog.service import encode
from media_tag.client.http import LibraryClientError
from media_tag.client.models import Acquisition,CurrentUseCheck
from media_tag.client.provider import TaskProvider
from media_tag.client.store import TaskStore
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.web.models import Enrollment
from mcp_adapter.support import facade
from search.test_public_api import run_cpu


class Response(BytesIO):
    def __init__(self,body,status,headers):
        super().__init__(body);self.status=status;self.headers=Message()
        for k,v in headers.items():self.headers[k]=v


class Opener:
    def __init__(self,app):self.client=TestClient(app);self.calls=[];self.mode=None
    def open(self,request,timeout=None):
        path=urlsplit(request.full_url).path+('?' + urlsplit(request.full_url).query if urlsplit(request.full_url).query else '')
        self.calls.append(request)
        response=self.client.request(request.get_method(),path,content=request.data,headers=dict(request.headers))
        if response.status_code>=400:raise HTTPError(request.full_url,response.status_code,'Synthetic',response.headers,BytesIO(response.content))
        body=response.content;headers=dict(response.headers);status=response.status_code
        if '/content?' in path:
            if self.mode=='interrupt':body=body[:len(body)//2]
            elif self.mode=='corrupt':body=b'Z'*len(body)
            elif self.mode=='wrong-range':headers['content-range']='bytes 0-1/2'
            elif self.mode=='wrong-etag':headers['etag']='"wrong"'
            elif self.mode=='ignore-range':
                response=self.client.get(path,headers={k:v for k,v in dict(request.headers).items() if k.lower() not in ('range','if-range')})
                status=response.status_code;body=response.content;headers=dict(response.headers)
        return Response(body,status,headers)


@pytest.fixture
def prepared(sought,tmp_path,tools):
    run_cpu(sought)
    jobs=tmp_path/'jobs';jobs.mkdir()
    client=facade(sought,tmp_path);client._opener=Opener(sought.app)
    provider=TaskProvider(client,jobs,'new-edit',tools,license_validation=False)
    transcript=b'{"segments":[],"synthetic":true}';candidate=b'{"synthetic":true}'
    (provider.store.task/'edit/cut.transcript.json').write_bytes(transcript)
    (provider.store.task/'edit/candidate.json').write_bytes(candidate)
    spec=Acquisition(asset_id=sought.asset.asset_id,sha256=sought.asset.sha256,context={
        'unit_id':'unit','transcript_sha256':sha256(transcript).hexdigest(),'candidate_sha256':sha256(candidate).hexdigest(),
        'quote':'Synthetic geometry only','purpose':'Show a red circle','timeline':{'start_seconds':0.0,'end_seconds':1.0}})
    return SimpleNamespace(provider=provider,spec=spec,sought=sought,jobs=jobs)


def check_for(p,result):
    value={'receipt_sha256':result['receipt_sha256'],'context':p.spec.context.model_dump(mode='json'),'source_sha256':p.spec.sha256,
        'decision':'pass','reviewer_type':'model','reviewed_by':'synthetic-contract-fixture','reviewed_at':datetime.now(timezone.utc).isoformat(),
        'actual_media_observed':True,'evidence':[{'path':result['receipt']['src'],'sha256':p.spec.sha256}],
        'checks':{k:{'status':'skipped' if k=='license' else 'pass','observation':'Synthetic protocol binding only, no actual AI host observation claimed by test.'}
            for k in ('semantic','action','timing','composition','packaging','technical','style','source','license','eye_mosaic')}}
    return value


def test_actual_bytes_are_verified_and_receipt_is_idempotent_but_reauthorized(prepared):
    p=prepared;first=p.provider.fetch(p.spec);second=p.provider.fetch(p.spec)
    assert first['receipt_sha256']==second['receipt_sha256'] and second['reused'] and not first['reused']
    assert Path(first['local_path']).read_bytes()==p.sought.original.read_bytes()
    assert first['receipt']['provider']=='media-tag' and not first['receipt']['host_registration_performed']
    assert first['receipt']['current_use_review']=='not_checked' and first['receipt']['decode']['whole_file_decode_passed']
    assert not first['receipt']['decode']['actual_media_observed']
    assert len([r for r in p.provider.client._opener.calls if '/download-manifest?' in r.full_url])==4
    assert len([r for r in p.provider.client._opener.calls if '/content?' in r.full_url])==1


@pytest.mark.parametrize('mode',['interrupt','ignore-range'])
def test_resume_checks_exact_range_or_restarts_same_version_without_append(prepared,mode):
    p=prepared;p.provider.client._opener.mode='interrupt'
    with pytest.raises(LibraryClientError,match='incomplete'):p.provider.fetch(p.spec)
    part=p.provider.store.path(p.spec.sha256+'.part');offset=part.stat().st_size
    assert 0<offset<p.sought.original.stat().st_size
    assert not list(p.provider.store.path('x').parent.glob('receipt-*.json'))
    p.provider.client._opener.mode='ignore-range' if mode=='ignore-range' else None
    result=p.provider.fetch(p.spec)
    assert result['resumed_bytes']==(0 if mode=='ignore-range' else offset)
    assert sha256(Path(result['local_path']).read_bytes()).hexdigest()==p.spec.sha256
    requests=[r for r in p.provider.client._opener.calls if '/content?' in r.full_url]
    assert requests[-1].get_header('Range')=='bytes='+str(offset)+'-' and requests[-1].get_header('If-match')=='"'+p.spec.sha256+'"'


@pytest.mark.parametrize('mode,code',[('corrupt','integrity_failed'),('wrong-etag','version_header_mismatch'),('wrong-range','range_mismatch')])
def test_corruption_and_wrong_headers_never_publish(prepared,mode,code):
    p=prepared
    if mode=='wrong-range':p.provider.store.path(p.spec.sha256+'.part').write_bytes(p.sought.original.read_bytes()[:12])
    p.provider.client._opener.mode=mode
    with pytest.raises(LibraryClientError,match=code):p.provider.fetch(p.spec)
    assert not list(p.provider.store.path('x').parent.glob('receipt-*.json'))
    assert not p.provider.store.path(p.spec.sha256+'.png').exists()
    p.provider.client._opener.mode=None
    assert p.provider.fetch(p.spec)['receipt']['sha256']==p.spec.sha256


def revoke(p):
    current=p.sought.env.catalog.get_asset(p.spec.asset_id,principal=p.sought.env.owner)
    p.sought.app.state.library.enroll(current.asset_id,Enrollment(request_id='revoke',expected_revision=current.revision,output_sha256=current.sha256,active=True),principal=p.sought.env.owner)


def test_cached_file_and_old_use_check_cannot_bypass_revocation(prepared):
    p=prepared;result=p.provider.fetch(p.spec);check=CurrentUseCheck.model_validate_json(encode(check_for(p,result)));revoke(p)
    for action in (lambda:p.provider.fetch(p.spec),lambda:p.provider.prepare_preview(check)):
        with pytest.raises(LibraryClientError) as error:action()
        assert error.value.status==403
    assert not list(p.provider.store.path('x').parent.glob('preview-handoff-*.json'))


def test_preview_handoff_binds_real_documents_evidence_and_preserves_policy(prepared):
    p=prepared;result=p.provider.fetch(p.spec);value=check_for(p,result);check=CurrentUseCheck.model_validate_json(encode(value))
    handoff=p.provider.prepare_preview(check)['handoff']
    assert handoff['preview_only'] and not handoff['final_export_allowed'] and handoff['medical_status']=='pending'
    assert handoff['provider']=='media-tag' and not handoff['apply_transform_again'] and not handoff['host_registration_performed']
    assert handoff['check_authenticity']=='caller_attestation_not_verified_by_adapter'
    value['context']['purpose']='Changed purpose'
    with pytest.raises(LibraryClientError,match='binding_mismatch'):p.provider.prepare_preview(CurrentUseCheck.model_validate_json(encode(value)))
    value=check_for(p,result);value['evidence'][0]['sha256']='f'*64
    with pytest.raises(LibraryClientError,match='evidence_hash_mismatch'):p.provider.prepare_preview(CurrentUseCheck.model_validate_json(encode(value)))
    (p.provider.store.task/'edit/cut.transcript.json').write_text('changed')
    with pytest.raises(LibraryClientError,match='document_hash_mismatch'):p.provider.prepare_preview(check)


def test_no_inferred_viewing_rejection_or_disabled_license_policy(prepared):
    p=prepared;result=p.provider.fetch(p.spec);value=check_for(p,result)
    for field,changed in (('actual_media_observed',False),('decision','reject')):
        bad={**value,field:changed}
        with pytest.raises(LibraryClientError,match='not_passed'):p.provider.prepare_preview(CurrentUseCheck.model_validate_json(encode(bad)))
    p.provider.license_validation=True
    with pytest.raises(LibraryClientError,match='cannot_be_skipped'):p.provider.prepare_preview(CurrentUseCheck.model_validate_json(encode(value)))
    value['checks']['source']['status']='skipped'
    with pytest.raises(ValueError):CurrentUseCheck.model_validate_json(encode(value))


def test_unmanaged_jobs_windows_names_traversal_and_hardlinks_are_refused(prepared,tmp_path):
    p=prepared;old=p.jobs/'old';old.mkdir();(old/'keep.txt').write_text('keep')
    with pytest.raises(LibraryClientError,match='unmanaged'):TaskStore(p.jobs,'old')
    for name in ('../escape','CON','NUL','trailing.'):
        with pytest.raises(ValueError):TaskStore(p.jobs,name)
    with pytest.raises(Exception):p.provider.store.path('../escape')
    outside=tmp_path/'outside';outside.write_text('read-only unrelated')
    os.link(outside,p.provider.store.path('linked.part'))
    with pytest.raises(LibraryClientError,match='unsafe_task_handle'):
        with p.provider.store.open('linked.part','r+b'):pass
    assert outside.read_text()=='read-only unrelated' and (old/'keep.txt').read_text()=='keep'


def test_job_lock_excludes_parallel_writers_and_releases_after_exception(prepared):
    p=prepared
    with p.provider.store.lock('same'):
        with pytest.raises(LibraryClientError,match='busy'):
            with p.provider.store.lock('same'):pass
    with p.provider.store.lock('same'):pass


def test_untrusted_manifest_content_is_never_fetched(prepared):
    p=prepared;original=p.provider.client.manifest
    def malicious(*args,**kwargs):return original(*args,**kwargs)|{'content':'https://outside.invalid/steal-token'}
    p.provider.client.manifest=malicious
    with pytest.raises(LibraryClientError,match='invalid_download_manifest'):p.provider.fetch(p.spec)
    assert not any('outside.invalid' in r.full_url for r in p.provider.client._opener.calls)


def test_cli_invalid_input_sanitizes_credentials_and_stays_in_task_scope(tmp_path):
    import subprocess,sys
    path=tmp_path/'bad.local.json';path.write_text('{"secret":"must-not-leak"}')
    result=subprocess.run([sys.executable,'-m','media_tag','acquire-media','--config',str(path),'--jobs-root',str(tmp_path/'jobs'),'--job-id','new','--input',str(path)],capture_output=True,timeout=10)
    assert result.returncode==2 and result.stdout==b'' and b'must-not-leak' not in result.stderr
    assert json.loads(result.stderr)['error']['code']=='task_adapter_failed'
