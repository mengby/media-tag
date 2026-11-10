from hashlib import sha256
import json
from types import SimpleNamespace
import pytest
from media_contracts import IntakeEvent,IntakeSpec,Blob,IntakeSource
from media_tag.upload.models import UploadSpec
from media_tag.sync.state import State
from media_tag.sync.client import SyncError
from media_tag.sync.bridge import Bridge


def event(number=1,**update):
    return IntakeEvent(event_id='event-'+str(number),feed_id='feed-one',sequence=number,upload_id='upload-a',actor_id='alice',filename='a.png',blob=Blob(sha256='a'*64,size_bytes=5),source=IntakeSource()).model_dump(mode='json')|update


def page(*items):return dict(feed_id='feed-one',items=list(items),next_cursor=items[-1]['sequence'] if items else 0)


def test_queue_before_cursor_restart_and_retry(tmp_path):
    s=State(tmp_path/'state',{'one':'scope'});s.receive_page(page(event(),event(2)))
    assert s.cursor()==2 and len(s.rows())==2
    s.update('event-1',last_error='transport_failed');s=State(tmp_path/'state',{'one':'scope'})
    assert s.cursor()==2 and s.rows()[0]['last_error']=='transport_failed'
    assert s.receive_page({'feed_id':'feed-one','items':[],'next_cursor':2})==0
    assert s.summary()['pending']==2
    with pytest.raises(SyncError,match='scope_changed'):State(tmp_path/'state',{'different':'scope'})


def test_page_is_atomic_and_feed_change_does_not_advance(tmp_path):
    s=State(tmp_path/'state',{})
    with pytest.raises(SyncError):s.receive_page(page(event(2),event(1)))
    assert s.cursor()==0 and not s.rows()
    s.receive_page(page(event()))
    with pytest.raises(SyncError,match='feed_changed'):s.receive_page({'feed_id':'feed-two','items':[],'next_cursor':1})
    assert s.cursor()==1


def test_lock_and_explicit_owner_binding(tmp_path):
    s=State(tmp_path/'state',{})
    with s.lock():
        with pytest.raises(SyncError,match='already_running'):
            with s.lock():pass
    with s.lock():pass
    s.bind_owner('alice','local-alice','http://127.0.0.1:1234')
    s.bind_owner('bob','local-bob','http://127.0.0.1:1234')
    with pytest.raises(SyncError,match='binding_changed'):s.bind_owner('alice','local-bob','http://127.0.0.1:1234')


@pytest.mark.parametrize('filename',['photo.png','中文图像.png','a-b.mp4'])
def test_accepted_cloud_contract_is_local_importable(filename):
    spec=IntakeSpec(request_id='one',filename=filename,blob=Blob(sha256='a'*64,size_bytes=1),source=IntakeSource(label='shared-test',reference='https://example.com/source'))
    UploadSpec.model_validate_json(json.dumps(dict(request_id='cloud-upload-one',filename=spec.filename,sha256=spec.blob.sha256,size_bytes=spec.blob.size_bytes,source=spec.source.model_dump(mode='json'))))


@pytest.mark.parametrize('name',['CON.png','NUL.mp4','foo.png.','LPT1.png','photo.png '])
def test_nonportable_cloud_names_rejected(name):
    with pytest.raises(ValueError):IntakeSpec(request_id='one',filename=name,blob=Blob(sha256='a'*64,size_bytes=1))


def test_whitespace_source_and_fake_stage_rejected():
    from media_contracts import SyncReceipt
    with pytest.raises(ValueError):IntakeSource(label='   ')
    with pytest.raises(ValueError):SyncReceipt(request_id='x',feed_id='f',event_id='e',phase='tagged',source_sha256='a'*64,local_asset_id='a',local_owner_id='o',classification='new_bytes')


@pytest.mark.parametrize('state',['completed','validating'])
def test_local_finalize_or_dispatch_crash_retries_complete(tmp_path,monkeypatch,state):
    from media_tag.sync import bridge as module
    calls=[];upload=dict(upload_id='upload-a',state=state,asset_id='asset-a',job_id=None)
    class Local:
        def __init__(self,config):pass
        def editor(self):return 'local-alice'
        def call(self,method,path,**kw):
            calls.append((method,path))
            if method=='POST':raise SyncError('complete_retry_observed')
            return upload
    monkeypatch.setattr(module,'IntakeClient',Local)
    b=Bridge.__new__(Bridge);b.settings=SimpleNamespace(owners={'alice':SimpleNamespace(local_actor_id='local-alice',client=SimpleNamespace(base_url='http://127.0.0.1:1234'))})
    b.state=State(tmp_path/'state',{});b.state.receive_page(page(event()));b.state.update('event-1',local_upload='upload-a')
    with pytest.raises(SyncError,match='complete_retry_observed'):b.import_event(b.state.row('event-1'))
    assert ('POST','/uploads/upload-a/complete') in calls
